from __future__ import annotations

import cmath
import math
import sys
from dataclasses import dataclass
from fractions import Fraction
from functools import reduce
from typing import TYPE_CHECKING

import numpy as np

from auto_editor.analyze import edit_method
from auto_editor.utils.func import apply_margin, boolop, cook, remove_small

if TYPE_CHECKING:
    from fractions import Fraction
    from typing import Any, Callable, Union

    from numpy.typing import NDArray

    from auto_editor.ffwrapper import FileInfo
    from auto_editor.output import Ensure
    from auto_editor.utils.bar import Bar
    from auto_editor.utils.log import Log

    Node = Union[Compound, ManyOp, Var, Num, Str, Bool, BoolArr]
    Number = Union[int, float, complex, Fraction]
    Real = Union[int, float, Fraction]
    BoolList = NDArray[np.bool_]


class MyError(Exception):
    pass


class Null:
    def __init__(self) -> None:
        pass

    def __eq__(self, obj: object) -> bool:
        return isinstance(obj, Null)

    def __str__(self) -> str:
        return "'()"

    __repr__ = __str__


class ConsType:
    __slots__ = ("a", "d")

    def __init__(self, a: Any, d: Any):
        self.a = a
        self.d = d

    def __repr__(self) -> str:
        result = f"({self.a}"
        tail = self.d
        while isinstance(tail, ConsType):
            result += f" {tail.a}"
            tail = tail.d

        return f"{result})" if isinstance(tail, Null) else f"{result} . {tail})"

    def __eq__(self, obj: object) -> bool:
        if isinstance(obj, ConsType):
            return self.a == obj.a and self.d == obj.d
        return False


class CharType:
    __slots__ = "val"

    def __init__(self, val: str):
        assert len(val) == 1
        self.val = val

    __str__: Callable[[CharType], str] = lambda self: self.val

    def __repr__(self) -> str:
        names = {" ": "space", "\n": "newline", "\t": "tab"}
        return f"#\\{self.val}" if self.val not in names else f"#\\{names[self.val]}"

    def __eq__(self, obj: object) -> bool:
        return self.val == obj.val if isinstance(obj, CharType) else False

    def __radd__(self, obj2: str) -> str:
        return obj2 + self.val


def print_arr(arr: BoolList) -> str:
    rs = "(boolarr"
    for item in arr:
        rs += " 1" if item else " 0"
    rs += ")\n"
    return rs


###############################################################################
#                                                                             #
#  LEXER                                                                      #
#                                                                             #
###############################################################################

METHODS = ("audio", "motion", "pixeldiff", "random", "none", "all")
SEC_UNITS = ("s", "sec", "secs", "second", "seconds")
ID, NUM, BOOL, STR, ARR, SEC, CHAR = "ID", "NUM", "BOOL", "STR", "ARR", "SEC", "CHAR"
LPAREN, RPAREN, LBRAC, RBRAC, LCUR, RCUR, EOF = "(", ")", "[", "]", "{", "}", "EOF"


class Token:
    __slots__ = ("type", "value")

    def __init__(self, type: str, value: Any):
        self.type = type
        self.value = value

    __str__: Callable[[Token], str] = lambda self: f"(Token {self.type} {self.value})"


class Lexer:
    __slots__ = ("log", "text", "pos", "char")

    def __init__(self, text: str):
        self.text = text
        self.pos: int = 0
        self.char = self.text[self.pos] if text else None

    def char_is_norm(self) -> bool:
        return self.char is not None and self.char not in '()[]{}"; \t\n\r\x0b\x0c'

    def advance(self) -> None:
        self.pos += 1
        self.char = None if self.pos > len(self.text) - 1 else self.text[self.pos]

    def peek(self) -> str | None:
        peek_pos = self.pos + 1
        return None if peek_pos > len(self.text) - 1 else self.text[peek_pos]

    def skip_whitespace(self) -> None:
        while self.char is not None and self.char in " \t\n\r\x0b\x0c":
            self.advance()

    def string(self) -> str:
        result = ""
        while self.char is not None and self.char != '"':
            if self.char == "\\":
                self.advance()
                if self.char in 'nt"\\':
                    if self.char == "n":
                        result += "\n"
                    if self.char == "t":
                        result += "\t"
                    if self.char == '"':
                        result += '"'
                    if self.char == "\\":
                        result += "\\"
                    self.advance()
                    continue

                if self.char is None:
                    raise MyError("Unexpected EOF while parsing")
                raise MyError(
                    f"Unexpected character {self.char} during escape sequence"
                )
            else:
                result += self.char
            self.advance()

        self.advance()
        return result

    def number(self) -> Token:
        result = ""
        token = NUM

        while self.char is not None and self.char in "+-0123456789./":
            result += self.char
            self.advance()

        unit = ""
        if self.char_is_norm():
            while self.char_is_norm():
                assert self.char is not None
                unit += self.char
                self.advance()

            if unit in SEC_UNITS:
                token = SEC
            elif unit != "i":
                return Token(ID, result + unit)

        if unit == "i":
            try:
                return Token(NUM, complex(f"{result}j"))
            except ValueError:
                return Token(ID, result + unit)

        if "/" in result:
            try:
                val = Fraction(result)
                if val.denominator == 1:
                    return Token(token, val.numerator)
                return Token(token, val)
            except ValueError:
                return Token(ID, result + unit)

        if "." in result:
            try:
                return Token(token, float(result))
            except ValueError:
                return Token(ID, result + unit)

        try:
            return Token(token, int(result))
        except ValueError:
            return Token(ID, result + unit)

    def hash_literal(self) -> Token:
        if self.char == "\\":
            self.advance()
            if self.char is None:
                raise MyError("Expected a character after #\\")

            char = self.char
            self.advance()
            return Token(CHAR, CharType(char))

        result = ""
        while self.char_is_norm():
            assert self.char is not None
            result += self.char
            self.advance()

        if result in ("t", "true"):
            return Token(BOOL, True)

        if result in ("f", "false"):
            return Token(BOOL, False)

        raise MyError(f"Unknown hash literal: {result}")

    def quote_literal(self) -> Token:
        result = ""
        if self.char == "(":
            result += self.char
            self.advance()
            while self.char is not None:
                result += self.char
                if self.char == ")":
                    self.advance()
                    break
                self.advance()

        if result == "()":
            return Token(ID, "null")

        raise MyError(f"Unknown quote literal: {result}")

    def get_next_token(self) -> Token:
        while self.char is not None:
            self.skip_whitespace()
            if self.char is None:
                continue

            if self.char == ";":
                while self.char is not None and self.char != "\n":
                    self.advance()
                continue

            if self.char == '"':
                self.advance()
                return Token(STR, self.string())

            if self.char in "(){}[]":
                _par = self.char
                self.advance()
                return Token(_par, _par)

            if self.char in "+-":
                _peek = self.peek()
                if _peek is not None and _peek in "0123456789.":
                    return self.number()

            if self.char in "0123456789.":
                return self.number()

            if self.char == "#":
                self.advance()
                return self.hash_literal()

            if self.char == "'":
                self.advance()
                return self.quote_literal()

            result = ""
            has_illegal = False
            while self.char_is_norm():
                result += self.char
                if self.char in "'`|\\":
                    has_illegal = True
                self.advance()

            if has_illegal:
                raise MyError(f"Symbol has illegal character(s): {result}")

            for method in METHODS:
                if result == method or result.startswith(f"{method}:"):
                    return Token(ARR, result)

            return Token(ID, result)

        return Token(EOF, "EOF")


###############################################################################
#                                                                             #
#  PARSER                                                                     #
#                                                                             #
###############################################################################


class Compound:
    __slots__ = "children"

    def __init__(self, children: list[Node]):
        self.children = children

    def __str__(self) -> str:
        s = "{Compound"
        for child in self.children:
            s += f" {child}"
        s += "}"
        return s


class ManyOp:
    __slots__ = ("op", "children")

    def __init__(self, op: Node, children: list[Node]):
        self.op = op
        self.children = children

    def __str__(self) -> str:
        s = f"(ManyOp {self.op}"
        for child in self.children:
            s += f" {child}"
        s += ")"
        return s

    __repr__ = __str__


class Var:
    def __init__(self, token: Token):
        assert token.type == ID
        self.token = token
        self.value = token.value

    __str__: Callable[[Var], str] = lambda self: f"(Var {self.value})"


class Num:
    __slots__ = "val"

    def __init__(self, val: int | float | Fraction | complex):
        self.val = val

    __str__: Callable[[Num], str] = lambda self: f"(num {self.val})"


class Bool:
    __slots__ = "val"

    def __init__(self, val: bool):
        self.val = val

    __str__: Callable[[Bool], str] = lambda self: f"(bool {'#t' if self.val else '#f'})"


class Str:
    __slots__ = "val"

    def __init__(self, val: str):
        self.val = val

    __str__: Callable[[Str], str] = lambda self: f"(str {self.val})"


class Char:
    __slots__ = "val"

    def __init__(self, val: str):
        self.val = val

    __str__: Callable[[Char], str] = lambda self: f"(char {self.val})"


class BoolArr:
    __slots__ = "val"

    def __init__(self, val: str):
        self.val = val

    __str__: Callable[[BoolArr], str] = lambda self: f"(boolarr {self.val})"


class Parser:
    def __init__(self, lexer: Lexer):
        self.lexer = lexer
        self.current_token = self.lexer.get_next_token()

    def eat(self, token_type: str) -> None:
        if self.current_token.type != token_type:
            raise MyError(f"Expected {token_type}, got {self.current_token.type}")

        self.current_token = self.lexer.get_next_token()

    def comp(self) -> Compound:
        comp_kids = []
        while self.current_token.type not in (EOF, RPAREN, RBRAC, RCUR):
            comp_kids.append(self.expr())
        return Compound(comp_kids)

    def expr(self) -> Node:
        token = self.current_token

        if token.type == ID:
            self.eat(ID)
            return Var(token)

        matches = {ARR: BoolArr, BOOL: Bool, NUM: Num, STR: Str, CHAR: Char}
        if token.type in matches:
            self.eat(token.type)
            return matches[token.type](token.value)

        if token.type == SEC:
            self.eat(SEC)
            return ManyOp(
                Var(Token(ID, "exact-round")),
                [
                    ManyOp(
                        Var(Token(ID, "*")),
                        [Num(token.value), Var(Token(ID, "timebase"))],
                    )
                ],
            )

        if token.type == LPAREN:
            self.eat(token.type)
            childs = []
            while self.current_token.type != RPAREN:
                if self.current_token.type == EOF:
                    raise MyError("Unexpected EOF")
                childs.append(self.expr())

            self.eat(RPAREN)
            return ManyOp(childs[0], children=childs[1:])

        if token.type == LBRAC:
            self.eat(token.type)
            childs = []
            while self.current_token.type != RBRAC:
                if self.current_token.type == EOF:
                    raise MyError("Unexpected EOF")
                childs.append(self.expr())

            self.eat(RBRAC)
            return ManyOp(childs[0], children=childs[1:])

        if token.type == LCUR:
            self.eat(token.type)
            childs = []
            while self.current_token.type != RCUR:
                if self.current_token.type == EOF:
                    raise MyError("Unexpected EOF")
                childs.append(self.expr())

            self.eat(RCUR)
            return ManyOp(childs[0], children=childs[1:])

        self.eat(token.type)
        childs = []
        while self.current_token.type not in (RPAREN, RBRAC, RCUR, EOF):
            childs.append(self.expr())

        return ManyOp(childs[0], children=childs[1:])

    def __str__(self) -> str:
        result = str(self.comp())

        self.lexer.pos = 0
        self.lexer.char = self.lexer.text[0]
        self.current_token = self.lexer.get_next_token()

        return result


###############################################################################
#                                                                             #
#  STANDARD LIBRARY                                                           #
#                                                                             #
###############################################################################


def check_args(
    o: str, values: list | tuple, arity: tuple[int, int | None], types: list[Any] | None
) -> None:
    lower, upper = arity
    amount = len(values)
    if upper is not None and lower > upper:
        raise ValueError("lower must be less than upper")
    if lower == upper and len(values) != lower:
        raise MyError(f"{o}: Arity mismatch. Expected {lower}, got {amount}")

    if upper is None and amount < lower:
        raise MyError(f"{o}: Arity mismatch. Expected at least {lower}, got {amount}")
    if upper is not None and (amount > upper or amount < lower):
        raise MyError(
            f"{o}: Arity mismatch. Expected between {lower} and {upper}, got {amount}"
        )

    if types is None:
        return

    for i, val in enumerate(values):
        check = types[-1] if i >= len(types) else types[i]
        if not check(val):
            raise MyError(f"{o} expects: {' '.join([_t.__doc__ for _t in types])}")


def is_boolarr(arr: object) -> bool:
    """boolarr?"""
    return arr.dtype.kind == "b" if isinstance(arr, np.ndarray) else False


def is_bool(val: object) -> bool:
    """boolean?"""
    return isinstance(val, bool)


def is_num(val: object) -> bool:
    """number?"""
    return not isinstance(val, bool) and isinstance(
        val, (int, float, Fraction, complex)
    )


def is_pair(val: object) -> bool:
    """pair?"""
    return isinstance(val, ConsType)


def is_real(val: object) -> bool:
    """real?"""
    return not isinstance(val, bool) and isinstance(val, (int, float, Fraction))


def is_eint(val: object) -> bool:
    """exact-integer?"""
    return not isinstance(val, bool) and isinstance(val, int)


def is_exact(val: object) -> bool:
    """exact?"""
    return isinstance(val, (int, Fraction))


def is_str(val: object) -> bool:
    """string?"""
    return isinstance(val, str)


def is_char(val: object) -> bool:
    """char?"""
    return isinstance(val, CharType)


def is_iterable(val: object) -> bool:
    """iterable?"""
    return isinstance(val, (list, range, np.ndarray, ConsType, Null))


def is_int(val: object) -> bool:
    """integer?"""
    if isinstance(val, float):
        return val.is_integer()
    return int(val) == val if isinstance(val, Fraction) else isinstance(val, int)


def raise_(msg: str) -> None:
    raise MyError(msg)


def display(val: Any) -> None:
    if val is None:
        return
    if is_boolarr(val):
        val = print_arr(val)
    sys.stdout.write(str(val))


def is_equal(a: object, b: object) -> bool:
    if isinstance(a, np.ndarray) and isinstance(b, np.ndarray):
        return np.array_equal(a, b)
    if isinstance(a, float) and not isinstance(b, float):
        return False
    return False if not isinstance(a, float) and isinstance(b, float) else a == b


def equal_num(*values: object) -> bool:
    return all(values[0] == val for val in values[1:])


def mul(*vals: Any) -> Number:
    return reduce(lambda a, b: a * b, vals, 1)


def minus(*vals: Number) -> Number:
    return -vals[0] if len(vals) == 1 else reduce(lambda a, b: a - b, vals)


def div(*vals: Any) -> Number:
    if len(vals) == 1:
        vals = (1, vals[0])
    try:
        if not {float, complex}.intersection({type(val) for val in vals}):
            result = reduce(lambda a, b: Fraction(a, b), vals)
            return result.numerator if result.denominator == 1 else result
        return reduce(lambda a, b: a / b, vals)
    except ZeroDivisionError:
        raise MyError("division by zero")


def _sqrt(v: Number) -> Number:
    r = cmath.sqrt(v)
    if r.imag == 0:
        return int(r.real) if int(r.real) == r.real else r.real
    return r


def ceiling(val: Real) -> Real:
    return float(math.ceil(val)) if isinstance(val, float) else math.ceil(val)


def floor(val: Real) -> Real:
    return float(math.floor(val)) if isinstance(val, float) else math.floor(val)


def _round(val: Real) -> Real:
    return float(round(val)) if isinstance(val, float) else round(val)


def _not(val: Any) -> bool | BoolList:
    if is_boolarr(val):
        return np.logical_not(val)
    if is_bool(val):
        return not val
    raise MyError("not expects: boolean? or boolarr?")


def _and(*vals: Any) -> bool | BoolList:
    if is_boolarr(vals[0]):
        check_args("and", vals, (2, None), [is_boolarr])
        return reduce(lambda a, b: boolop(a, b, np.logical_and), vals)
    check_args("and", vals, (1, None), [is_bool])
    return reduce(lambda a, b: a and b, vals)


def _or(*vals: Any) -> bool | BoolList:
    if is_boolarr(vals[0]):
        check_args("or", vals, (2, None), [is_boolarr])
        return reduce(lambda a, b: boolop(a, b, np.logical_or), vals)
    check_args("or", vals, (1, None), [is_bool])
    return reduce(lambda a, b: a or b, vals)


def _xor(*vals: Any) -> bool | BoolList:
    if is_boolarr(vals[0]):
        check_args("xor", vals, (2, None), [is_boolarr])
        return reduce(lambda a, b: boolop(a, b, np.logical_xor), vals)
    check_args("xor", vals, (2, None), [is_bool])
    return reduce(lambda a, b: a ^ b, vals)


def string_append(*vals: str | CharType) -> str:
    return reduce(lambda a, b: a + b, vals, "")


def string_ref(s: str, ref: int) -> CharType:
    try:
        return CharType(s[ref])
    except IndexError:
        raise MyError(f"string index {ref} is out of range")


def number_to_string(val: Number) -> str:
    if isinstance(val, complex):
        join = "" if val.imag < 0 else "+"
        return f"{val.real}{join}{val.imag}i"
    return f"{val}"


def length(val: Any) -> int:
    if isinstance(val, (ConsType, Null)):
        count = 0
        while isinstance(val, ConsType):
            val = val.d
            count += 1
        if not isinstance(val, Null):
            raise MyError("length expects: list?")
        return count

    return len(val)


def minclip(arr: BoolList, _min: int) -> BoolList:
    return remove_small(np.copy(arr), _min, replace=1, with_=0)


def mincut(arr: BoolList, _min: int) -> BoolList:
    return remove_small(np.copy(arr), _min, replace=0, with_=1)


def margin(a: Any, b: Any, c: Any = None) -> BoolList:
    if c is None:
        check_args("margin", [a, b], (2, 2), [is_eint, is_boolarr])
        arr = b
        start, end = a, a
    else:
        check_args("margin", [a, b, c], (3, 3), [is_eint, is_eint, is_boolarr])
        arr = c
        start, end = a, b
    return apply_margin(np.copy(arr), len(arr), start, end)


def _list(*values: Any) -> ConsType | Null:
    result: ConsType | Null = Null()
    for val in reversed(values):
        result = ConsType(val, result)
    return result


def list_ref(result: ConsType, ref: int) -> Any:
    if ref < 0:
        raise MyError(f"{ref}: Invalid index")
    while ref > 0:
        ref -= 1
        result = result.d
        if isinstance(result, Null):
            raise MyError(f"{ref}: Invalid index")
        if not isinstance(result, ConsType):
            raise MyError("list-ref: 1st arg must be a list")
    return result.a


def listq(val: Any) -> bool:
    while isinstance(val, ConsType):
        val = val.d
    return isinstance(val, Null)


###############################################################################
#                                                                             #
#  INTERPRETER                                                                #
#                                                                             #
###############################################################################


@dataclass
class FileSetup:
    src: FileInfo
    ensure: Ensure
    strict: bool
    tb: Fraction
    bar: Bar
    temp: str
    log: Log


@dataclass
class Proc:
    name: str
    proc: Callable
    arity: tuple[int, int | None] = (1, None)
    contracts: list[Any] | None = None

    def __str__(self) -> str:
        return f"#<procedure:{self.name}>"

    __repr__ = __str__


class Interpreter:

    GLOBAL_SCOPE: dict[str, Any] = {
        # constants
        "true": True,
        "false": False,
        "null": Null(),
        "pi": math.pi,
        # actions
        "begin": Proc("begin", lambda *x: None if not x else x[-1], (0, None)),
        "display": Proc("display", display, (1, 1)),
        "exit": Proc("exit", sys.exit, (1, None)),
        "error": Proc("error", raise_, (1, 1), [is_str]),
        # booleans
        ">": Proc(">", lambda a, b: a > b, (2, 2), [is_real, is_real]),
        ">=": Proc(">=", lambda a, b: a >= b, (2, 2), [is_real, is_real]),
        "<": Proc("<", lambda a, b: a < b, (2, 2), [is_real, is_real]),
        "<=": Proc("<=", lambda a, b: a <= b, (2, 2), [is_real, is_real]),
        "=": Proc("=", equal_num, (1, None), [is_num]),
        "not": Proc("not", _not, (1, 1)),
        "and": Proc("and", _and, (1, None)),
        "or": Proc("or", _or, (1, None)),
        "xor": Proc("xor", _xor, (2, None)),
        # questions
        "equal?": Proc("equal?", is_equal, (2, 2)),
        "list?": Proc("list?", listq, (1, 1)),
        "pair?": Proc("pair?", is_pair, (1, 1)),
        "null?": Proc("null?", lambda val: isinstance(val, Null), (1, 1)),
        "number?": Proc("number?", is_num, (1, 1)),
        "exact?": Proc("exact?", is_exact, (1, 1)),
        "inexact?": Proc("inexact?", lambda v: not is_exact(v), (1, 1)),
        "real?": Proc("real?", is_real, (1, 1)),
        "integer?": Proc("integer?", is_int, (1, 1)),
        "exact-integer?": Proc("exact-integer?", is_eint, (1, 1)),
        "positive?": Proc("positive?", lambda v: v > 0, (1, 1), [is_real]),
        "negative?": Proc("negative?", lambda v: v < 0, (1, 1), [is_real]),
        "zero?": Proc("zero?", lambda v: v == 0, (1, 1), [is_real]),
        "boolean?": Proc("boolean?", is_bool, (1, 1)),
        "string?": Proc("string?", is_str, (1, 1)),
        "char?": Proc("char?", is_char, (1, 1)),
        # cons/list
        "cons": Proc("cons", lambda a, b: ConsType(a, b), (2, 2)),
        "car": Proc("car", lambda val: val.a, (1, 1), [is_pair]),
        "cdr": Proc("cdr", lambda val: val.d, (1, 1), [is_pair]),
        "list": Proc("list", _list, (0, None)),
        "list-ref": Proc("list-ref", list_ref, (2, 2), [is_pair, is_eint]),
        "length": Proc("length", length, (1, 1), [is_iterable]),
        # strings
        "string": Proc("string", string_append, (0, None), [is_char]),
        "string-append": Proc("string-append", string_append, (0, None), [is_str]),
        "string-upcase": Proc("string-upcase", lambda s: s.upper(), (1, 1), [is_str]),
        "string-downcase": Proc(
            "string-downcase", lambda s: s.lower(), (1, 1), [is_str]
        ),
        "string-titlecase": Proc(
            "string-titlecase", lambda s: s.title(), (1, 1), [is_str]
        ),
        "string-length": Proc("string-length", len, (1, 1), [is_str]),
        "string-ref": Proc("string-ref", string_ref, (2, 2), [is_str, is_eint]),
        "number->string": Proc("number->string", number_to_string, (1, 1), [is_num]),
        # numbers
        "+": Proc("+", lambda *v: sum(v), (0, None), [is_num]),
        "-": Proc("-", minus, (1, None), [is_num]),
        "*": Proc("*", mul, (0, None), [is_num]),
        "/": Proc("/", div, (1, None), [is_num]),
        "add1": Proc("add1", lambda v: v + 1, (1, 1), [is_num]),
        "sub1": Proc("sub1", lambda v: v - 1, (1, 1), [is_num]),
        "expt": Proc("expt", pow, (2, 2), [is_real]),
        "sqrt": Proc("sqrt", _sqrt, (1, 1), [is_num]),
        "mod": Proc("mod", lambda a, b: a % b, (2, 2), [is_int, is_int]),
        "modulo": Proc("modulo", lambda a, b: a % b, (2, 2), [is_int, is_int]),
        "real-part": Proc("real-part", lambda v: v.real, (1, 1), [is_num]),
        "imag-part": Proc("imag-part", lambda v: v.imag, (1, 1), [is_num]),
        # reals
        "abs": Proc("abs", abs, (1, 1), [is_real]),
        "ceil": Proc("ceil", ceiling, (1, 1), [is_real]),
        "ceiling": Proc("ceiling", ceiling, (1, 1), [is_real]),
        "exact-ceil": Proc("exact-ceil", math.ceil, (1, 1), [is_real]),
        "exact-ceiling": Proc("exact-ceiling", math.ceil, (1, 1), [is_real]),
        "floor": Proc("floor", floor, (1, 1), [is_real]),
        "exact-floor": Proc("exact-floor", math.floor, (1, 1), [is_real]),
        "round": Proc("round", _round, (1, 1), [is_real]),
        "exact-round": Proc("exact-round", round, (1, 1), [is_real]),
        "max": Proc("max", lambda *v: max(v), (1, None), [is_real]),
        "min": Proc("min", lambda *v: min(v), (1, None), [is_real]),
        # ae extensions
        "margin": Proc("margin", margin, (2, 3), None),
        "mcut": Proc("mincut", mincut, (2, 2), [is_eint, is_boolarr]),
        "mincut": Proc("mincut", mincut, (2, 2), [is_eint, is_boolarr]),
        "mclip": Proc("minclip", minclip, (2, 2), [is_eint, is_boolarr]),
        "minclip": Proc("minclip", minclip, (2, 2), [is_eint, is_boolarr]),
        "cook": Proc(
            "cook",
            lambda a, b, c: cook(np.copy(c), b, a),
            (3, 3),
            [is_eint, is_eint, is_boolarr],
        ),
        "boolarr": Proc(
            "boolarr", lambda *a: np.array(a, dtype=np.bool_), (1, None), [is_eint]
        ),
        "count-nonzero": Proc("count-nonzero", np.count_nonzero, (1, 1), [is_boolarr]),
        "boolarr?": Proc("boolarr?", is_boolarr, (1, 1)),
    }

    def __init__(self, parser: Parser, filesetup: FileSetup | None):
        self.parser = parser
        self.filesetup = filesetup

        if filesetup is not None:
            self.GLOBAL_SCOPE["timebase"] = filesetup.tb

    def visit(self, node: Node) -> Any:
        if isinstance(node, (Num, Str, Bool, Char)):
            return node.val

        if isinstance(node, Var):
            val = self.GLOBAL_SCOPE.get(node.value)
            if val is None:
                raise MyError(f"{node.value} is undefined")
            return val

        if isinstance(node, BoolArr):
            if self.filesetup is None:
                raise MyError("Can't use edit methods if there's no input files")
            return edit_method(node.val, self.filesetup)

        if isinstance(node, ManyOp):
            name = node.op.value if isinstance(node.op, Var) else None
            if name == "if":
                check_args("if", node.children, (3, 3), None)
                test_expr = self.visit(node.children[0])
                if not isinstance(test_expr, bool):
                    raise MyError("if: test-expr arg must be: boolean?")
                if test_expr:
                    return self.visit(node.children[1])
                return self.visit(node.children[2])

            if name == "when":
                check_args("when", node.children, (2, 2), None)
                test_expr = self.visit(node.children[0])
                if not isinstance(test_expr, bool):
                    raise MyError("when: test-expr arg must be: boolean?")
                if test_expr:
                    return self.visit(node.children[1])
                return None

            if name in ("define", "set!"):
                check_args(name, node.children, (2, 2), None)

                if not isinstance(node.children[0], Var):
                    raise MyError(
                        f"Variable must be set with a symbol, got {node.children[0]}"
                    )

                var_name = node.children[0].value
                if name == "set!" and var_name not in self.GLOBAL_SCOPE:
                    raise MyError(f"Cannot set variable {var_name} before definition")

                self.GLOBAL_SCOPE[var_name] = self.visit(node.children[1])
                return None

            if not isinstance(oper := self.visit(node.op), Proc):
                raise MyError(f"{oper}, expected procedure")

            values = [self.visit(child) for child in node.children]
            check_args(oper.name, values, oper.arity, oper.contracts)
            return oper.proc(*values)

        if isinstance(node, Compound):
            return [self.visit(child) for child in node.children]
        raise ValueError(f"Unknown node type: {node}")

    def interpret(self) -> Any:
        return self.visit(self.parser.comp())
