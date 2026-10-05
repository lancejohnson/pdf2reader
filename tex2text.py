"""Turn simple TeX math into readable Unicode text (Readwise Reader can't render MathML or TeX)."""
import re

SYM = {r"\sum": "Σ", r"\infty": "∞", r"\times": "×", r"\cdot": "·", r"\Delta": "Δ", r"\delta": "δ",
       r"\beta": "β", r"\alpha": "α", r"\sigma": "σ", r"\mu": "μ", r"\pi": "π", r"\approx": "≈",
       r"\leq": "≤", r"\geq": "≥", r"\le": "≤", r"\ge": "≥", r"\neq": "≠", r"\pm": "±", r"\%": "%",
       r"\$": "$", r"\,": " ", r"\;": " ", r"\quad": "  ", r"\ldots": "…", r"\dots": "…", r"\to": "→",
       r"\rightarrow": "→", r"\left": "", r"\right": "", r"\displaystyle": "", r"-": "−"}
SUB = str.maketrans("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")
SUP = str.maketrans("0123456789+-−=()intxkT", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁱⁿᵗˣᵏᵀ")
VULGAR = {("1", "2"): "½", ("1", "4"): "¼", ("3", "4"): "¾", ("1", "3"): "⅓", ("2", "3"): "⅔", ("1", "8"): "⅛"}


def _group(s, i):
    """Return (content, next_index) of a {...} group or single token at s[i]."""
    if i < len(s) and s[i] == "{":
        depth, j = 0, i
        while j < len(s):
            depth += {"{": 1, "}": -1}.get(s[j], 0)
            if depth == 0:
                return s[i + 1:j], j + 1
            j += 1
        return s[i + 1:], len(s)
    m = re.match(r"\\[A-Za-z]+|.", s[i:])
    return (m.group(0), i + len(m.group(0))) if m else ("", i)


def _wrap(x):
    x = x.strip()
    return x if re.fullmatch(r"[\w.∞]+|\(.*\)", x) and x.count("(") == x.count(")") else f"({x})"


def _script(x, table):
    x = x.replace(" ", "")
    return x.translate(table) if all(ch in table_chars(table) for ch in x) else None


def table_chars(t):
    return {chr(k) for k in t}


def convert(tex):
    s, out, i = tex, [], 0
    while i < len(s):
        if s.startswith(r"\frac", i) or s.startswith(r"\dfrac", i) or s.startswith(r"\tfrac", i):
            i = s.index("frac", i) + 4
            a, i = _group(s, i)
            b, i = _group(s, i)
            if (a.strip(), b.strip()) in VULGAR:
                out.append(VULGAR[(a.strip(), b.strip())])
            else:
                out.append(f"{_wrap(convert(a))}/{_wrap(convert(b))}")
        elif s.startswith((r"\text", r"\mathrm", r"\textrm", r"\mathit", r"\operatorname"), i):
            i = s.index("{", i)
            a, i = _group(s, i)
            out.append(a)
        elif s.startswith(r"\sqrt", i):
            a, i = _group(s, i + 5)
            out.append(f"√{_wrap(convert(a))}")
        elif re.match(r"\\(sum|prod)_", s[i:]):
            op = "Σ" if s.startswith(r"\sum", i) else "Π"
            lo, i = _group(s, i + len(op == "Σ" and r"\sum" or r"\prod") + 1)
            hi = ""
            if i < len(s) and s[i] == "^":
                hi, i = _group(s, i + 1)
            out.append(f"{op}({convert(lo)} to {convert(hi)}) " if hi else f"{op}({convert(lo)}) ")
        elif s[i] in "_^":
            table = SUB if s[i] == "_" else SUP
            a, i = _group(s, i + 1)
            body = convert(a)
            small = _script(body, table)
            if small is not None:
                out.append(small)
            elif table is SUB:
                out.append(f"_{body}" if len(body) == 1 else f"_({body})")
            else:
                out.append(f"^{body}" if len(body) == 1 else f"^({body})")
        elif s[i] == "\\":
            m = re.match(r"\\([A-Za-z]+|.)", s[i:])
            tok = m.group(0)
            out.append(SYM.get(tok, tok[1:]))
            i += len(tok)
        elif s[i] in "{}":
            i += 1
        else:
            out.append(SYM.get(s[i], s[i]))
            i += 1
    txt = "".join(out)
    # Σ with limits: "Σ₍ₜ₌₀₎^∞" style -> "Σ (t=0 to ∞)"
    return re.sub(r"\s+", " ", txt).strip()


if __name__ == "__main__":
    import sys
    for t in sys.argv[1:]:
        print(convert(t))
