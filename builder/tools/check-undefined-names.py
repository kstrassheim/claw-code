#!/usr/bin/env python3
"""Fail on a NAME that is loaded but never bound in its scope.

`python -m py_compile` accepts an undefined name happily — it is a runtime
NameError, not a syntax error. That is not academic here: a planner that
references a variable which does not exist in that function crashes on EVERY
tick, and because the CronJob's only visible symptom is a stack trace in a
log nobody reads, the subsystem is simply dead until someone looks.

That happened: the issue-solver planner referenced `pod` where the variable
is called `openclaw_pod`, and the solver did nothing at all between one
deploy and the next.

No third-party linter is assumed — these images and this checkout have no
pyflakes/ruff — so this is a deliberately small AST pass over builder/*.py.
It binds module-level names, builtins, function arguments (including those of
NESTED functions, whose bodies are walked as part of the enclosing one),
assignments, imports, except-handler names, comprehension targets and
global/nonlocal declarations. Anything still unbound is reported.
"""
import ast, builtins, glob, sys

BUILTINS = set(dir(builtins))


def _bound_in(fn: ast.AST) -> set[str]:
    bound: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
            a = node.args
            for arg in (a.posonlyargs + a.args + a.kwonlyargs
                        + ([a.vararg] if a.vararg else [])
                        + ([a.kwarg] if a.kwarg else [])):
                bound.add(arg.arg)
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                bound.add((al.asname or al.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.comprehension):
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name):
                    bound.add(t.id)
    return bound


def main(paths: list[str]) -> int:
    findings = []
    for path in sorted(paths):
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
        module = _bound_in(tree) | BUILTINS
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            bound = module | _bound_in(fn)
            for n in ast.walk(fn):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) \
                        and n.id not in bound:
                    findings.append(f"{path}:{n.lineno}: in {fn.name}(): "
                                    f"undefined name '{n.id}'")
    for f in sorted(set(findings)):
        print(f)
    if findings:
        print(f"\n{len(set(findings))} undefined name(s)")
        return 1
    print(f"no undefined names in {len(paths)} file(s)")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:] or sorted(glob.glob("builder/*.py"))
    raise SystemExit(main(args))
