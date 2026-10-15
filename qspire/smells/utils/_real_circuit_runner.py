"""Subprocess runner for RealCircuitTracker.

Executes a Q-Spire generated executable (or any Python file) with the *real*
Qiskit QuantumCircuit class and returns, as JSON on stdout, the operations of
every circuit created while the analyzed code runs.

Why: the previous tracker rebuilt circuits by scanning every source line of the
generated script with regular expressions, grouping lines by variable name. The
generated script contains the analyzed body twice (as a function and inlined in
the main block) and also the bodies of all other functions of the file, so
circuits with the same variable name were merged and operations were counted
twice. Executing the code and reading QuantumCircuit.data avoids both problems,
handles loops, registers, parameters, and control flow, and keeps one circuit
per QuantumCircuit object.

Usage: python _real_circuit_runner.py <file> [per_statement_timeout_s]
Output: a line starting with '@@QSPIRE_JSON@@' followed by the JSON result.
"""
import ast
import builtins
import json
import os
import re
import signal
import sys

MARK = "@@QSPIRE_JSON@@"


class _StatementTimeout(BaseException):
    pass


def _alarm_handler(signum, frame):
    raise _StatementTimeout()


def _max_int(rep):
    """Largest integer index in a qubit/clbit specifier, or None."""
    try:
        import numpy as np  # noqa
        int_types = (int, np.integer)
    except Exception:
        int_types = (int,)
    if isinstance(rep, bool):
        return None
    if isinstance(rep, int_types):
        return int(rep)
    if isinstance(rep, range):
        return max(rep) if len(rep) else None
    if isinstance(rep, (list, tuple)):
        vals = [_max_int(r) for r in rep]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None
    return None


def main():
    script = os.path.abspath(sys.argv[1])
    per_stmt_timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (6 * 1024 ** 3, 6 * 1024 ** 3))
    except Exception:
        pass

    from qiskit.circuit import QuantumCircuit, QuantumRegister, ClassicalRegister, Gate, Instruction
    from qiskit.circuit.exceptions import CircuitError

    src = open(script, encoding="utf-8").read()
    src_lines = src.split("\n")
    # Keep the real QuantumCircuit: neutralize the mock alias of the generated scripts.
    src_exec = re.sub(r"(?m)^QuantumCircuit[ \t]*=[ \t]*MockQuantumCircuit[ \t]*$",
                      "pass  # qspire-fix: real QuantumCircuit", src)
    try:
        tree = ast.parse(src_exec)
    except SyntaxError as e:
        print(MARK + json.dumps({"circuits": {}, "errors": [f"SyntaxError: {e}"]}))
        return

    def is_main_if(node):
        if not isinstance(node, ast.If):
            return False
        t = node.test
        return (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id == "__name__"
                and len(t.comparators) == 1 and isinstance(t.comparators[0], ast.Constant)
                and t.comparators[0].value == "__main__")

    pre, main_stmts = [], None
    for node in tree.body:
        if main_stmts is None and is_main_if(node):
            main_stmts = list(node.body)
        else:
            pre.append(node)
    if main_stmts is None:  # plain script: the whole module is the analyzed code
        main_stmts, pre = pre, [n for n in pre if isinstance(n, (ast.Import, ast.ImportFrom))]
        main_stmts = [n for n in main_stmts if not isinstance(n, (ast.Import, ast.ImportFrom))]

    state = {"main": False}
    registry = []          # (circuit, creation line)
    lines_of = {}          # id(circuit) -> {instruction index: line}

    def script_line(depth=2):
        f = sys._getframe(depth)
        while f is not None:
            if os.path.abspath(f.f_code.co_filename) == script:
                return f.f_lineno
            f = f.f_back
        return None

    # --- register circuits created by the analyzed code -----------------------------
    orig_init = QuantumCircuit.__init__

    def patched_init(self, *a, **k):
        orig_init(self, *a, **k)
        if state["main"]:
            f = sys._getframe(1)
            if os.path.abspath(f.f_code.co_filename) == script:
                registry.append((self, f.f_lineno))
    QuantumCircuit.__init__ = patched_init

    # --- grow circuits when synthetic sizes are too small -----------------------------
    def make_conv(orig, kind):
        def conv(self, rep):
            try:
                return orig(self, rep)
            except CircuitError:
                idx = _max_int(rep)
                size = self.num_qubits if kind == "q" else self.num_clbits
                if idx is not None and size <= idx < 4096:
                    reg_cls = QuantumRegister if kind == "q" else ClassicalRegister
                    self.add_register(reg_cls(idx + 1 - size, f"_qsx{kind}{len(self.qregs) + len(self.cregs)}"))
                    return orig(self, rep)
                raise
        return conv
    QuantumCircuit._qbit_argument_conversion = make_conv(QuantumCircuit._qbit_argument_conversion, "q")
    QuantumCircuit._cbit_argument_conversion = make_conv(QuantumCircuit._cbit_argument_conversion, "c")

    # --- remember the source line of each appended instruction ------------------------
    def wrap_append(meth):
        def w(self, *a, **k):
            try:
                n0 = len(self._data)
            except Exception:
                n0 = None
            r = meth(self, *a, **k)
            try:
                n1 = len(self._data)
                line = script_line(2)
                if n0 is not None and line is not None and n1 > n0:
                    d = lines_of.setdefault(id(self), {})
                    for i in range(n0, n1):
                        d[i] = line
            except Exception:
                pass
            return r
        return w
    QuantumCircuit.append = wrap_append(QuantumCircuit.append)
    QuantumCircuit._append_standard_gate = wrap_append(QuantumCircuit._append_standard_gate)

    # --- execute ----------------------------------------------------------------------
    ns = {"__name__": "__main__", "__file__": script, "__builtins__": builtins}
    errors = []
    try:
        signal.signal(signal.SIGALRM, _alarm_handler)
        use_alarm = True
    except Exception:
        use_alarm = False

    def run(node):
        code = compile(ast.Module(body=[node], type_ignores=[]), script, "exec")
        if use_alarm:
            signal.alarm(per_stmt_timeout)
        try:
            exec(code, ns)
        finally:
            if use_alarm:
                signal.alarm(0)

    for node in pre:
        try:
            run(node)
        except BaseException as e:  # noqa
            errors.append(f"pre l.{getattr(node, 'lineno', '?')}: {type(e).__name__}")
    state["main"] = True
    for node in main_stmts:
        try:
            run(node)
        except SystemExit:
            break  # a `return` of the analyzed function (rewritten as sys.exit by Q-Spire)
        except _StatementTimeout:
            errors.append(f"main l.{node.lineno}: timeout")
        except BaseException as e:  # noqa
            errors.append(f"main l.{node.lineno}: {type(e).__name__}: {str(e)[:120]}")
    state["main"] = False

    # --- export circuits --------------------------------------------------------------
    def expandable(op):
        return type(op) in (Gate, Instruction) and getattr(op, "definition", None) is not None

    def ops_of(circ, inst_iter, qmap, cmap, line_for, depth=0):
        out = []
        for i, inst in inst_iter:
            op = inst.operation
            qs = [qmap(q) for q in inst.qubits]
            cs = [cmap(c) for c in inst.clbits]
            if depth < 10 and expandable(op):
                d = op.definition
                sub_q = {id(b): qs[j] for j, b in enumerate(d.qubits)}
                sub_c = {id(b): cs[j] for j, b in enumerate(d.clbits)}
                out.extend(ops_of(d, enumerate(d.data), lambda b: sub_q[id(b)], lambda b: sub_c[id(b)],
                                  lambda _i, _l=line_for(i): _l, depth + 1))
                continue
            line = line_for(i)
            text = src_lines[line - 1] if line and 0 < line <= len(src_lines) else ""
            out.append({
                "operation_name": op.name,
                "qubits_affected": qs,
                "clbits_affected": cs,
                "row": line,
                "column_start": len(text) - len(text.lstrip()),
                "column_end": len(text.rstrip()),
                "source_line": text.strip(),
            })
        return out

    names = {}
    for k, v in ns.items():
        if isinstance(v, QuantumCircuit) and id(v) not in names:
            names[id(v)] = k
    result, seen = {}, set()
    for circ, cline in registry:
        if id(circ) in seen:
            continue
        seen.add(id(circ))
        try:
            lmap = lines_of.get(id(circ), {})
            ops = ops_of(circ, enumerate(circ.data), lambda b: circ.find_bit(b).index,
                         lambda b: circ.find_bit(b).index, lambda i: lmap.get(i, cline))
        except Exception as e:  # noqa
            errors.append(f"export: {type(e).__name__}")
            continue
        if not ops:
            continue
        name = names.get(id(circ))
        if not name:
            m = re.match(r"\s*(?:self\.)?(\w+)\s*(?::[^=]+)?=", src_lines[cline - 1]) if 0 < cline <= len(src_lines) else None
            name = m.group(1) if m else "circuit"
        key, n = name, 2
        while key in result:
            key, n = f"{name}#{n}", n + 1
        result[key] = ops

    sys.stdout.flush()
    print(MARK + json.dumps({"circuits": result, "errors": errors}))


if __name__ == "__main__":
    main()
