"""Utilities for lowering subgraphs used by higher order operators

"""

import functools
import operator
from dataclasses import dataclass
from typing import List, TypeVar

import torch

from . import ir
from .exc import SubgraphLoweringException
from .ops_handler import SimpleCSEHandler
from .virtualized import ops, V, WrapperHandler

T = TypeVar("T")


class PointwiseSubgraphLowering(torch.fx.Interpreter):
    graph_outputs: List[ir.IRNode]

    def __init__(
        self,
        gm: torch.fx.GraphModule,
        root_graph_lowering: "torch._inductor.graph.GraphLowering",
    ):
        super().__init__(gm)
        self.graph_outputs = []
        self.root_graph = root_graph_lowering

    @property
    def sizevars(self):
        return self.root_graph.sizevars

    def mark_buffer_mutated(self, name):
        raise SubgraphLoweringException("Mutations are not supported in this context")

    def register_buffer(self, data):
        raise SubgraphLoweringException(
            "Buffer creation is not supported in this context"
        )

    def call_function(self, target, args, kwargs):
        from .lowering import lowerings

        if target is operator.getitem and isinstance(args[0], (list, tuple, dict)):
            return super().call_function(target, args, kwargs)

        assert isinstance(target, torch._ops.OpOverload)

        if target not in lowerings:
            raise SubgraphLoweringException(
                f"{target} not supported in subgraph, (missing lowering)"
            )

        if torch.Tag.pointwise not in target.tags:
            raise SubgraphLoweringException(
                f"Only pointwise operators are supported in this context, but got {target}"
            )

        return lowerings[target](*args, **kwargs)

    def output(self, target, args, kwargs):
        self.graph_outputs.extend(args)


@dataclass
class InputDescriptor:
    dtype: torch.dtype
    device: torch.device


class TracingOpsHandler(WrapperHandler[T]):
    def __init__(self, tracer):
        parent = tracer.create_proxy("placeholder", "ops", (), {})
        super().__init__(parent)
        self.tracer = tracer

    def placeholder(self, name):
        return self.tracer.create_proxy("placeholder", name, (), {})

    def output(self, *args):
        return self.tracer.create_node(
            "output", "output", tuple(self.tracer.create_arg(a) for a in args), {}
        )


def lower_pointwise_subgraph(gm: torch.fx.GraphModule, inputs: List[InputDescriptor]):
    # Lower subgraph to ir.Pointwise nodes
    def fake_inner_fn(idx, name):
        return ops.placeholder(name)

    graph_inputs = [
        ir.Pointwise.create(
            device=desc.device,
            dtype=desc.dtype,
            inner_fn=functools.partial(fake_inner_fn, name=f"input{i}"),
            ranges=[],
        )
        for i, desc in enumerate(inputs)
    ]
    subgraph = PointwiseSubgraphLowering(gm, root_graph_lowering=V.graph)
    with V.set_graph_handler(subgraph):  # type: ignore[arg-type]
        subgraph.run(*graph_inputs)

    # Combine multiple pointwise computations into a single graph module
    # Do this by tracing through each individually and doing CSE
    tracer = torch.fx.Tracer()
    tracer.graph = torch.fx.Graph(tracer_cls=tracer.__class__)
    trace_ops = SimpleCSEHandler(TracingOpsHandler(tracer))

    outputs = subgraph.graph_outputs

    with V.set_ops_handler(trace_ops):
        output_irs = []

        for out_var in subgraph.graph_outputs:
            assert isinstance(out_var, ir.TensorBox)
            assert out_var.get_size() == []
            assert isinstance(out_var.data, ir.StorageBox)
            assert isinstance(out_var.data.data, ir.Pointwise)

            idx = ()
            ir_out = out_var.data.data.inner_fn(idx)

            output_irs.append(ir_out)

        ops.output(*output_irs)

    lowered_gm = torch.fx.GraphModule({}, tracer.graph)

    def inner_fn(*args, **kwargs):
        return lowered_gm(V.get_ops_handler(), *args, **kwargs)

    return inner_fn
