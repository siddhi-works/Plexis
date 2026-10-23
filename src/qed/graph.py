"""Task graphs on LangGraph: independent steps run at the same time, dependents wait.

`Graph` is a thin builder over a LangGraph `StateGraph`. Each node is an async
function of the results so far; nodes without dependencies start together
from START, and a node with several dependencies joins on all of them
(LangGraph's multi-source edge). Every node records when it started and
finished, which the simulator draws as a timeline. An optional node may fail
without stopping its dependents (research, for example).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

Fn = Callable[[dict[str, Any]], Awaitable[Any]]


def _merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**left, **right}


class _State(TypedDict, total=False):
    results: Annotated[dict[str, Any], _merge]  # parallel branches each add their own key


@dataclass
class Node:
    name: str
    fn: Fn
    deps: tuple[str, ...] = ()
    optional: bool = False


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)

    def add(self, name: str, fn: Fn, *, deps: tuple[str, ...] = (), optional: bool = False) -> Graph:
        if name in self.nodes:
            raise ValueError(f"duplicate node {name}")
        unknown = [d for d in deps if d not in self.nodes]
        if unknown:
            raise ValueError(f"{name} depends on unknown node(s) {unknown}; add dependencies first")
        self.nodes[name] = Node(name, fn, deps, optional)
        return self

    def build(self, trace: dict[str, dict[str, Any]], origin: float) -> Any:
        sg = StateGraph(_State)
        for node in self.nodes.values():
            sg.add_node(node.name, self._wrap(node, trace, origin))
            if node.deps:
                sg.add_edge(list(node.deps) if len(node.deps) > 1 else node.deps[0], node.name)
            else:
                sg.add_edge(START, node.name)
        has_dependents = {d for n in self.nodes.values() for d in n.deps}
        for name in self.nodes:
            if name not in has_dependents:
                sg.add_edge(name, END)
        return sg.compile()

    @staticmethod
    def _wrap(
        node: Node, trace: dict[str, dict[str, Any]], origin: float
    ) -> Callable[[_State], Awaitable[_State]]:
        async def run(state: _State) -> _State:
            start = time.perf_counter()
            rec = trace[node.name] = {
                "name": node.name,
                "deps": list(node.deps),
                "startMs": round((start - origin) * 1000, 2),
            }
            try:
                value = await node.fn(dict(state.get("results") or {}))
            except Exception as exc:
                rec.update(status="failed", error=str(exc)[:120])
                if node.optional:
                    return {"results": {}}
                raise
            finally:
                rec["durationMs"] = round((time.perf_counter() - start) * 1000, 2)
            rec["status"] = "ok"
            return {"results": {node.name: value}}

        return run

    async def run(self, *, t0: float | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        origin = time.perf_counter() if t0 is None else t0
        trace: dict[str, dict[str, Any]] = {}
        final = await self.build(trace, origin).ainvoke({"results": {}})
        return final.get("results") or {}, [trace[n] for n in self.nodes if n in trace]
