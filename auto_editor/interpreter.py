from __future__ import annotations

from fractions import Fraction
from functools import reduce
from math import ceil, floor
from typing import TYPE_CHECKING

import numpy as np

from auto_editor.analyze import (
    audio_levels,
    get_all,
    get_none,
    motion_levels,
    pixeldiff_levels,
    random_levels,
    to_threshold,
)
from auto_editor.objs.edit import (
    Audio,
    Motion,
    Pixeldiff,
    Random,
    audio_builder,
    motion_builder,
    pixeldiff_builder,
    random_builder,
)
from auto_editor.objs.util import _Vars, parse_dataclass
from auto_editor.utils.func import apply_margin, cook, remove_small

if TYPE_CHECKING:
    from fractions import Fraction
    from typing import Callable, Union

    from numpy.typing import NDArray

    from auto_editor.ffwrapper import FileInfo
    from auto_editor.output import Ensure
    from auto_editor.utils.bar import Bar
    from auto_editor.utils.log import Log

    BoolList = NDArray[np.bool_]
    BoolOperand = Callable[[BoolList, BoolList], BoolList]

    # None represents NoOp
    Node = Union[Compound, UnOp, BinOp, TerOp, ManyOp, Proc, Num, Str, Bool, BoolArrV]


def boolop(a: BoolList, b: BoolList, call: BoolOperand) -> BoolList:
    if len(a) > len(b):
        b = np.resize(b, len(a))
    if len(b) > len(a):
        a = np.resize(a, len(b))

    return call(a, b)


def is_boolarr(arr: object) -> bool:
    if isinstance(arr, np.ndarray):
        return arr.dtype.kind == "b"
    return False


def is_num(val: object) -> bool:
    return isinstance(val, (int, float, Fraction))


###############################################################################
#                                                                             #
#  LEXER                                                                      #
#                                                                             #
###############################################################################

abc = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ!\"#$%&'*+,_-./:;<=>?@\\^`|~"

METHODS = ("audio", "motion", "pixeldiff", "random", "none", "all")
SEC_UNITS = ("s", "sec", "secs", "second", "seconds")
METHOD_ATTRS_SEP = ":"

NUM, STR, ARR, SEC, LPAREN, RPAREN, EOF = "NUM", "STR", "ARR", "SEC", "(", ")", "EOF"
NOT, OR, AND, XOR, BOOL = "NOT", "OR", "AND", "XOR", "BOOL"
PLUS, MINUS, MUL, DIV = "PLUS", "MINUS", "MUL", "DIV"
ROND, EROND, CEIL, ECEIL, FLR, EFLR = "ROND", "EROND", "CEIL", "ECEIL", "FLR", "EFLR"
A1, S1, MOD = "A1", "S1", "MOD"
SA, SU, SD, ST = "SA", "SU", "SD", "ST"

NUMQ, STRQ, BOOLQ, BOOLARRQ = (
    "NUMQ",
    "STRQ",
    "BOOLQ",
    "BOOLARRQ",
)
EQN, GR, LT, GRE, LTE = "EQN", "GR", "LT", "GRE", "LTE"
POS, NEG, ZERO, EQ = "POS", "NEG", "ZERO", "EQ"

MARGIN, MCUT, MCLIP, COOK, BOOLARR = "MARGIN", "MCUT", "MCLIP", "COOK", "BOOLARR"

func_map = {
    "not": NOT,
    "or": OR,
    "and": AND,
    "xor": XOR,
    "+": PLUS,
    "-": MINUS,
    "*": MUL,
    "/": DIV,
    ">": GR,
    ">=": GRE,
    "<": LT,
    "<=": LTE,
    "=": EQN,
    "round": ROND,
    "exact-round": EROND,
    "ceiling": CEIL,
    "exact-ceiling": ECEIL,
    "floor": FLR,
    "exact-floor": EFLR,
    "modulo": MOD,
    "add1": A1,
    "sub1": S1,
    "string-append": SA,
    "string-upcase": SU,
    "string-downcase": SD,
    "string-titlecase": ST,
    "number?": NUMQ,
    "string?": STRQ,
    "boolean?": BOOLQ,
    "positive?": POS,
    "negative?": NEG,
    "zero?": ZERO,
    "equal?": EQ,
    # ae extensions
    "margin": MARGIN,
    "mcut": MCUT,
    "mincut": MCUT,
    "mclip": MCLIP,
    "minclip": MCLIP,
    "cook": COOK,
    "boolarr": BOOLARR,
    "boolarr?": BOOLARRQ,
}


class Token:
    __slots__ = ("type", "value")

    def __init__(self, type: str, value: str):
        self.type = type
        self.value = value

    def __str__(self) -> str:
        return f"(Token {self.type} {self.value})"


class Lexer:
    __slots__ = ("log", "text", "pos", "char")

    def __init__(self, text: str):
        self.text = text
        self.pos: int = 0
        if len(text) == 0:
            self.char: str | None = None
        else:
            self.char = self.text[self.pos]

    def advance(self) -> None:
        self.pos += 1
        if self.pos > len(self.text) - 1:
            self.char = None
        else:
            self.char = self.text[self.pos]

    def peek(self):
        peek_pos = self.pos + 1
        if peek_pos > len(self.text) - 1:
            return None
        else:
            return self.text[peek_pos]

    def skip_whitespace(self) -> None:
        while self.char is not None and self.char in " \t\n\r\x0b\x0c":
            self.advance()

    def num_or_unit(self) -> int | float:
        result = ""
        numerator = None
        has_dot = False
        sign = 1
        if self.char == "-":
            sign = -1
            self.advance()

        while self.char is not None and self.char in "0123456789./":
            if self.char == "/":
                if numerator is not None:
                    raise TypeError("Too many /'s in Fraction literal")
                try:
                    numerator = int(result)
                except ValueError:
                    raise TypeError(f"Numerator '{result}' cannot be converted to int")
                result = ""
                self.advance()
            elif self.char == ".":
                if has_dot:
                    raise TypeError("Too many .'s in float literal")
                has_dot = True
                result += self.char
                self.advance()
            else:
                result += self.char
                self.advance()

        token = NUM

        if self.char is not None and self.char in abc:
            unit = ""
            while self.char is not None and self.char in abc:
                unit += self.char
                self.advance()

            if unit not in SEC_UNITS:
                raise TypeError(f"Unknown unit: {unit}")
            token = SEC

        if numerator is not None:
            return Token(token, Fraction(numerator * sign, int(result)))

        if has_dot:
            return Token(token, float(result) * sign)
        return Token(token, int(result) * sign)

    def string(self) -> str:
        result = ""
        while self.char is not None and self.char != '"':
            if self.char == "\\":
                self.advance()
                if self.char in 'nt"\\':
                    result += f"\\{self.char}"
                    self.advance()
                    continue

                if self.char is None:
                    raise TypeError("Unexpected EOF while parsing")
                raise TypeError(f"Unexpected character {nc} during escape sequence")
            else:
                result += self.char
            self.advance()

        self.advance()
        return result

    def symbol(self) -> tuple[str, str]:
        result = ""
        while self.char is not None and self.char in abc:
            result += self.char
            self.advance()

        if result in ("#t", "true"):
            return BOOL, "true"

        if result in ("#f", "false"):
            return BOOL, "false"

        if result in func_map:
            return func_map[result], result

        for method in METHODS:
            if result.startswith(method):
                return ARR, result

        raise TypeError(f"Unknown name: '{result}'")

    def get_next_token(self) -> Token:
        while self.char is not None:
            self.skip_whitespace()

            if self.char == '"':
                self.advance()
                return Token(STR, self.string())

            if self.char == "(":
                self.advance()
                return Token(LPAREN, "(")

            if self.char == ")":
                self.advance()
                return Token(RPAREN, ")")

            if self.char == "-":
                if self.peek() is not None and self.peek() in "0123456789.":
                    return self.num_or_unit()

            if self.char in "0123456789.":
                return self.num_or_unit()

            return Token(*self.symbol())

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


class UnOp:
    __slots__ = ("op", "value")

    def __init__(self, op: Proc, value: Node):
        self.op = op
        self.value = value

    def __str__(self) -> str:
        return f"(UnOp {self.op} {self.value})"


class BinOp:
    __slots__ = ("op", "first", "last")

    def __init__(self, op: Proc, first: Node, last: Node):
        self.op = op
        self.first = first
        self.last = last

    def __str__(self) -> str:
        return f"(BinOp {self.op} {self.first} {self.last})"


class TerOp:
    __slots__ = ("op", "first", "middle", "last")

    def __init__(self, op: Proc, first: Node, middle: Node, last: Node):
        self.op = op
        self.first = first
        self.middle = middle
        self.last = last

    def __str__(self) -> str:
        return f"(TerOp {self.op} {self.first} {self.middle} {self.last})"


class ManyOp:
    __slots__ = ("op", "children")

    def __init__(self, op: Proc, children: list[Node]):
        self.op = op
        self.children = children

    def __str__(self) -> str:
        s = f"(ManyOp {self.op}"
        for child in self.children:
            s += f" {child}"
        s += ")"
        return s


class Atom:
    pass


class Proc(Atom):
    __slots__ = "op"

    def __init__(self, op: Token):
        assert isinstance(op, Token)
        self.type = op.type
        self.name: str = op.value

    def __str__(self) -> str:
        return f"(Proc {self.name})"

    def __repr__(self) -> str:
        return f"#<procedure:{self.name}>"


class Num(Atom):
    __slots__ = "val"

    def __init__(self, val):
        assert isinstance(val, (int, float, Fraction))
        self.val = val

    def __str__(self) -> str:
        return f"(num {self.val})"


class Bool(Atom):
    __slots__ = "val"

    def __init__(self, val):
        assert isinstance(val, bool)
        self.val = val

    def __str__(self) -> str:
        b = "#t" if self.val else "#f"
        return f"(bool {b})"


class Str(Atom):
    __slots__ = "val"

    def __init__(self, val):
        assert isinstance(val, str)
        self.val = val

    def __str__(self) -> str:
        return f"(str {self.val})"


class BoolArrV(Atom):
    __slots__ = "val"

    def __init__(self, val: str):
        self.val = val

    def __str__(self) -> str:
        return f"(boolarr {self.val})"


class Parser:
    def __init__(self, lexer: Lexer, tb: Fraction):
        self.lexer = lexer
        self.tb = tb
        self.current_token = self.lexer.get_next_token()

        self.un_ops = {
            # math
            A1,
            S1,
            # strings
            SD,
            SU,
            ST,
            # rounding
            ROND,
            EROND,
            CEIL,
            ECEIL,
            FLR,
            EFLR,
            # bools
            NUMQ,
            STRQ,
            BOOLQ,
            BOOLARRQ,
            POS,
            NEG,
            ZERO,
            NOT,
        }
        self.bin_ops = {MOD, MARGIN, MCUT, MCLIP, EQ, EQN, GR, LT, GRE, LTE}
        self.ter_ops = {MARGIN, COOK}
        self.many_ops = {SA, OR, AND, XOR, PLUS, MINUS, MUL, DIV, BOOLARR}
        self.all_ops = (
            self.many_ops.union(self.un_ops).union(self.bin_ops).union(self.ter_ops)
        )

    def eat(self, token_type: str) -> None:
        if self.current_token.type != token_type:
            raise TypeError(f"Expected {token_type}, got {self.current_token.type}")

        self.current_token = self.lexer.get_next_token()

    def comp(self) -> list[Node]:
        comp_kids = []
        while self.current_token.type not in (EOF, RPAREN):
            comp_kids.append(self.expr())
        return Compound(comp_kids)

    def expr(self) -> Node:
        token = self.current_token

        if token.type == ARR:
            self.eat(ARR)
            return BoolArrV(token.value)

        if token.type == BOOL:
            self.eat(BOOL)
            return Bool(token.value == "true")

        if token.type == NUM:
            self.eat(NUM)
            return Num(token.value)

        if token.type == STR:
            self.eat(STR)
            return Str(token.value)

        if token.type == SEC:
            self.eat(SEC)
            return UnOp(
                Proc(Token(EROND, "exact-round")),
                ManyOp(Proc(Token(MUL, "*")), [Num(token.value), Num(self.tb)]),
            )

        if token.type == LPAREN:
            self.eat(LPAREN)
            node = self.expr()
            self.eat(RPAREN)
            return node

        while self.current_token.type in self.all_ops:
            token = self.current_token
            self.eat(token.type)

            childs = []
            while self.current_token.type not in (RPAREN, EOF):
                childs.append(self.expr())

            if token.type in self.many_ops:
                return ManyOp(Proc(token), children=childs)

            if len(childs) == 1:
                if token.type not in self.un_ops:
                    raise TypeError(
                        f"{token.value} has wrong number of expressions. got {len(childs)}"
                    )
                return UnOp(Proc(token), childs[0])

            if len(childs) == 2:
                if token.type not in self.bin_ops:
                    raise TypeError(
                        f"{token.value} has wrong number of expressions. got {len(childs)}"
                    )
                return BinOp(Proc(token), childs[0], childs[1])

            if len(childs) == 3:
                if token.type not in self.ter_ops:
                    raise TypeError(
                        f"{token.value} has wrong number of expressions. got {len(childs)}"
                    )
                return TerOp(Proc(token), childs[0], childs[1], childs[2])

        raise TypeError("Unexpected token type")


    def __str__(self) -> str:
        result = str(self.comp())

        self.lexer.pos = 0
        self.lexer.char = self.lexer.text[0]
        self.current_token = self.lexer.get_next_token()

        return result


###############################################################################
#                                                                             #
#  INTERPRETER                                                                #
#                                                                             #
###############################################################################


class Interpreter:
    def __init__(
        self,
        parser: Parser,
        src: FileInfo,
        ensure: Ensure,
        strict: bool,
        tb: Fraction,
        bar: Bar,
        temp: str,
        log: Log,
    ):

        self.parser = parser
        self.src = src
        self.ensure = ensure
        self.strict = strict
        self.tb = tb
        self.bar = bar
        self.temp = temp
        self.log = log

    def visit(self, node: Node) -> Any:
        if isinstance(node, Atom):
            if isinstance(node, (Num, Str, Bool)):
                return node.val

            if isinstance(node, BoolArrV):
                src, ensure, strict, tb = self.src, self.ensure, self.strict, self.tb
                bar, temp, log = self.bar, self.temp, self.log

                if METHOD_ATTRS_SEP in node.val:
                    method, attrs = node.val.split(METHOD_ATTRS_SEP)
                    if method not in METHODS:
                        log.error(f"'{method}' not allowed to have attributes")
                else:
                    method, attrs = node.val, ""

                if method == "none":
                    return get_none(ensure, src, tb, temp, log)

                if method == "all":
                    return get_all(ensure, src, tb, temp, log)

                if method == "random":
                    robj = parse_dataclass(attrs, (Random, random_builder), log)
                    return to_threshold(
                        random_levels(ensure, src, robj, tb, temp, log), robj.threshold
                    )

                if method == "audio":
                    aobj = parse_dataclass(attrs, (Audio, audio_builder), log)
                    s = aobj.stream
                    if s == "all":
                        total_list: BoolList | None = None
                        for s in range(len(src.audios)):
                            audio_list = to_threshold(
                                audio_levels(
                                    ensure, src, s, tb, bar, strict, temp, log
                                ),
                                aobj.threshold,
                            )
                            if total_list is None:
                                total_list = audio_list
                            else:
                                total_list = boolop(
                                    total_list, audio_list, np.logical_or
                                )
                        if total_list is None:
                            if strict:
                                log.error("Input has no audio streams.")
                            stream_data = get_all(ensure, src, tb, temp, log)
                        else:
                            stream_data = total_list
                    else:
                        stream_data = to_threshold(
                            audio_levels(ensure, src, s, tb, bar, strict, temp, log),
                            aobj.threshold,
                        )

                    return stream_data

                if method == "motion":
                    if src.videos:
                        _vars: _Vars = {"width": src.videos[0].width}
                    else:
                        _vars = {"width": 1}

                    mobj = parse_dataclass(attrs, (Motion, motion_builder), log, _vars)
                    return to_threshold(
                        motion_levels(ensure, src, mobj, tb, bar, strict, temp, log),
                        mobj.threshold,
                    )

                if method == "pixeldiff":
                    pobj = parse_dataclass(attrs, (Pixeldiff, pixeldiff_builder), log)
                    return to_threshold(
                        pixeldiff_levels(ensure, src, pobj, tb, bar, strict, temp, log),
                        pobj.threshold,
                    )

                raise ValueError("Unreachable")

            raise ValueError("Unreachable")

        if isinstance(node, UnOp):
            val = self.visit(node.value)
            operator = node.op.name

            if node.op.type == NUMQ:
                return is_num(val)
            if node.op.type == STRQ:
                return isinstance(val, str)
            if node.op.type == BOOLQ:
                return isinstance(val, bool)
            if node.op.type == BOOLARRQ:
                return is_boolarr(val)

            if node.op.type == ZERO and is_num(val):
                return val == 0
            if node.op.type == POS and is_num(val):
                return val > 0
            if node.op.type == NEG and is_num(val):
                return val < 0

            if node.op.type == CEIL:
                if isinstance(val, float):
                    return float(ceil(val))
                if isinstance(val, (int, Fraction)):
                    return ceil(val)

            if node.op.type == ECEIL and is_num(val):
                return ceil(val)

            if node.op.type == FLR:
                if isinstance(val, float):
                    return float(floor(val))
                if isinstance(val, (int, Fraction)):
                    return floor(val)

            if node.op.type == EFLR and is_num(val):
                return floor(val)

            if node.op.type == ROND:
                if isinstance(val, float):
                    return float(round(val))
                if isinstance(val, (int, Fraction)):
                    return round(val)

            if node.op.type == EROND and is_num(val):
                return round(val)

            if node.op.type == NOT:
                if is_boolarr(val):
                    return np.logical_not(val)

                if isinstance(val, bool):
                    return not val

            if is_num(val):
                if node.op.type == A1:
                    return val + 1
                if node.op.type == S1:
                    return val - 1

            if isinstance(val, str):
                if node.op.type == SU:
                    return val.upper()
                if node.op.type == SD:
                    return val.lower()
                if node.op.type == ST:
                    return val.title()

            if val is None:
                raise TypeError(f"{operator} needs a value")

            raise TypeError(f"{operator} got value in wrong type: '{val}'")

        if isinstance(node, BinOp):
            first = self.visit(node.first)
            last = self.visit(node.last)
            operator = node.op.name

            if node.op.type == EQ:
                if isinstance(first, float) and not isinstance(last, float):
                    return False
                if isinstance(last, float) and not isinstance(first, float):
                    return False
                return first == last

            if node.op.type == GR:
                if is_num(first) and is_num(last):
                    return first > last
                raise TypeError(f"{operator} expects <num, num>")

            if node.op.type == GRE:
                if is_num(first) and is_num(last):
                    return first >= last
                raise TypeError(f"{operator} expects <num, num>")

            if node.op.type == LT:
                if is_num(first) and is_num(last):
                    return first < last
                raise TypeError(f"{operator} expects <num, num>")

            if node.op.type == LTE:
                if is_num(first) and is_num(last):
                    return first <= last
                raise TypeError(f"{operator} expects <num, num>")

            if node.op.type == EQN:
                if is_num(first) and is_num(last):
                    return first == last
                raise TypeError(f"{operator} expects <num, num>")

            if node.op.type == MCLIP:
                if isinstance(first, int) and is_boolarr(last):
                    return remove_small(last, first, replace=1, with_=0)
                raise TypeError(f"{operator} expects <int, boolarr>")

            if node.op.type == MCUT:
                if isinstance(first, int) and is_boolarr(last):
                    return remove_small(last, first, replace=0, with_=1)
                raise TypeError(f"{operator} expects <int, boolarr>")

            if node.op.type == MARGIN:
                if isinstance(first, int) and is_boolarr(last):
                    _len = len(last)
                    return apply_margin(last, _len, first, first)

                raise TypeError(f"{operator} expects <int, boolarr>")

            if node.op.type == MOD:
                if isinstance(first, int) and isinstance(last, int):
                    return first % last

                raise TypeError(f"{operator} expects <int, int>")

            raise ValueError("Unreachable")

        if isinstance(node, TerOp):
            first = self.visit(node.first)
            middle = self.visit(node.middle)
            last = self.visit(node.last)
            operator = node.op.name

            if node.op.type == COOK:
                if (
                    isinstance(first, int)
                    and isinstance(middle, int)
                    and is_boolarr(last)
                ):
                    # (cook mincut minclip boolarr)
                    return cook(last, middle, first)

                raise TypeError(f"{operator} expects <int, int, boolarr>")

            if node.op.type == MARGIN:
                if (
                    isinstance(first, int)
                    and isinstance(middle, int)
                    and is_boolarr(last)
                ):
                    _len = len(last)
                    return apply_margin(last, _len, first, middle)

                raise TypeError(f"{operator} expects <int, int, boolarr>")

            raise ValueError("Unreachable")

        if isinstance(node, ManyOp):
            values = []
            all_bools = True
            all_ints = True
            all_boolarrs = True
            for child in node.children:
                _val = self.visit(child)

                if not isinstance(_val, np.ndarray):
                    all_boolarrs = False

                if not isinstance(_val, bool):
                    all_bools = False

                if isinstance(_val, float):
                    all_ints = False
                values.append(_val)

            if len(values) == 0:
                return node.op

            if node.op.type == SA:
                return reduce(lambda a, b: a + b, values)

            if all_bools:
                if node.op.type == OR:
                    return reduce(lambda a, b: a or b, values)
                if node.op.type == AND:
                    return reduce(lambda a, b: a and b, values)
                if node.op.type == XOR:
                    return reduce(lambda a, b: a ^ b, values)

            if all_boolarrs:
                if node.op.type == OR:
                    return reduce(lambda a, b: boolop(a, b, np.logical_or), values)
                if node.op.type == AND:
                    return reduce(lambda a, b: boolop(a, b, np.logical_and), values)
                if node.op.type == XOR:
                    return reduce(lambda a, b: boolop(a, b, np.logical_xor), values)

            if node.op.type == PLUS:
                return reduce(lambda a, b: a + b, values)
            if node.op.type == MINUS:
                return reduce(lambda a, b: a - b, values)
            if node.op.type == MUL:
                return reduce(lambda a, b: a * b, values)
            if node.op.type == DIV:
                if len(values) == 1:
                    values.insert(0, 1)
                if all_ints:
                    return reduce(lambda a, b: Fraction(a, b), values)
                return reduce(lambda a, b: a / b, values)

            raise ("Operator got wrong type. This error message does not know the correct type")

        if isinstance(node, Compound):
            results = []
            for child in node.children:
                results.append(self.visit(child))
            return results

        raise ValueError(f"Unknown node type: {node}")

    def interpret(self):
        return self.visit(self.parser.comp())


def run_interpreter(
    text: str,
    src: FileInfo,
    ensure: Ensure,
    strict: bool,
    tb: Fraction,
    bar: Bar,
    temp: str,
    log: Log,
) -> BoolList:

    try:
        lexer = Lexer(text)
        parser = Parser(lexer, tb)
        if log.debug:
            log.debug(f"edit: {parser}")

        interpreter = Interpreter(parser, src, ensure, strict, tb, bar, temp, log)
        result = interpreter.interpret()
    except TypeError as e:
        log.error(e)

    assert isinstance(result, np.ndarray)
    return result
