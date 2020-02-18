import logging
import re
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Set, Union

import pysbd
from TexSoup import RArg, TexNode, TexSoup, TokenWithPosition

from explanations.scan_tex import (
    EndOfInput,
    Match,
    Pattern,
    TexScanner,
    has_balanced_braces,
    scan_tex,
)
from explanations.types import (
    BeginDocument,
    Bibitem,
    Documentclass,
    Entity,
    Equation,
    LengthAssignment,
    Macro,
    MacroDefinition,
)

LEFT_BRACE = Pattern("left_brace", r"\{")
RIGHT_BRACE = Pattern("right_brace", r"\}")


@dataclass(frozen=True)
class NamedEnv:
    name: str
    star: bool
    arg_pattern: str = ""


@dataclass(frozen=True)
class DelimitedEnv:
    delimiter: str


@dataclass(frozen=True)
class StartEndEnv:
    start: str
    end: str


EnvSpec = Union[DelimitedEnv, NamedEnv, StartEndEnv]


"""
List of math environments from: https://latex.wikia.org/wiki/List_of_LaTeX_environments
TODO(andrewhead): Support 'alignat' and 'matrix'.
TODO(andrewhead): Determine if any other environments besides 'array' have arguments.
"""
MATH_ENVIRONMENT_SPECS: Dict[str, EnvSpec] = {
    # Inline math
    "dollar": DelimitedEnv(r"\$(?!\$)"),
    "parens": StartEndEnv(r"\\\(", r"\\\)"),
    "math": NamedEnv("math", star=False),
    # Display math
    "dollardollar": DelimitedEnv(r"\$\$"),
    "bracket": StartEndEnv(r"\\\[", r"\\\]"),
    "displaymath": NamedEnv("displaymath", star=True),
    "equation": NamedEnv("equation", star=True),
    "split": NamedEnv("split", star=True),
    "array": NamedEnv("array", arg_pattern=r"(?:\{[^}]*\})?", star=True),
    "eqnarray": NamedEnv("eqnarray", star=True),
    "multiline": NamedEnv("multiline", star=True),
    "gather": NamedEnv("gather", star=True),
    "align": NamedEnv("align", star=True),
    "flalign": NamedEnv("flalign", star=True),
}


def begin_environment_regex(name: str, arg_pattern: str = "") -> str:
    return r"\\begin{" + name + r"}" + r"(\s*" + arg_pattern + ")"


def end_environment_regex(name: str) -> str:
    return r"\\end{" + name + r"}"


def make_math_environment_patterns() -> List[Pattern]:

    begin = begin_environment_regex
    end = end_environment_regex

    patterns: List[Pattern] = []
    for name, spec in MATH_ENVIRONMENT_SPECS.items():
        if isinstance(spec, DelimitedEnv):
            patterns.append(Pattern(name + "_delimiter", spec.delimiter))
        elif isinstance(spec, StartEndEnv):
            patterns.append(Pattern(name + "_start", spec.start))
            patterns.append(Pattern(name + "_end", spec.end))
        elif isinstance(spec, NamedEnv):
            patterns.append(
                Pattern(name + "_start", begin(spec.name, spec.arg_pattern))
            )
            patterns.append(Pattern(name + "_end", end(spec.name)))
            if spec.star:
                patterns.append(
                    Pattern(
                        name + "s_start", begin(spec.name + r"\*", spec.arg_pattern)
                    )
                )
                patterns.append(Pattern(name + "s_end", end(spec.name + r"\*")))
    return patterns


class EquationExtractor:
    """
    TODO(andrewhead): Cases that this doesn't yet handle:
    * Nested dollar signs: "$x + \\hbox{$y$}$"
    """

    def __init__(self) -> None:
        self.PATTERNS = make_math_environment_patterns()

    def parse(self, tex: str) -> Iterator[Equation]:

        self._stack: List[Match] = []  # pylint: disable=attribute-defined-outside-init
        self._tex = tex  # pylint: disable=attribute-defined-outside-init
        self._equation_index = 0  # pylint: disable=attribute-defined-outside-init

        scanner = scan_tex(tex, self.PATTERNS)
        for match in scanner:
            for equation in self._process_token(match):
                yield equation

    def _process_token(self, match: Match) -> Iterator[Equation]:
        pattern_name = match.pattern.name

        if pattern_name.endswith("_start"):
            self._stack.append(match)

        elif self._in_environment(pattern_name):
            start_pattern_name = self._get_start_pattern_name(pattern_name)
            while self._stack[-1].pattern.name != start_pattern_name:
                self._stack.pop()
            start_match = self._stack.pop()

            depth = len(self._stack)
            equation_tex = self._tex[start_match.start : match.end]
            content_tex = self._tex[start_match.end : match.start]
            yield Equation(
                start_match.start,
                match.end,
                self._equation_index,
                equation_tex,
                start_match.end,
                match.start,
                content_tex,
                depth,
            )
            self._equation_index += 1

        elif pattern_name.endswith("_delimiter"):
            self._stack.append(match)

    def _get_start_pattern_name(self, end_pattern_name: str) -> str:
        if end_pattern_name.endswith("_delimiter"):
            return end_pattern_name
        return re.sub("_end$", "_start", end_pattern_name)

    def _in_environment(self, end_pattern_name: str) -> bool:
        start_pattern_name = self._get_start_pattern_name(end_pattern_name)
        return any([m.pattern.name == start_pattern_name for m in self._stack])


"""
Valid units of measure in TeX and related engines.
"""
LENGTH_UNITS = [
    # From The TeXBook, "Chapter 10: Dimensions", p57.
    "pt",
    "pc",
    "in",
    "bp",
    "cm",
    "mm",
    "dd",
    "cc",
    "sp",
    # From the LaTeX book on Wikibooks: https://en.wikibooks.org/wiki/LaTeX/Lengths
    "ex",
    "em",
    "nd",
    "nc",
]

"""
Parameters for laying out arrays in LaTeX. From "The LaTeX2E Sources" by Braams et al.,
version 2019-10-01 Patch Level 1, page 341.
"""
ARRAY_PARAMETERS = [
    "arraycolsep",
    "tabcolsep",
    "arrayrulewidth",
    "doublerulesep",
    "arraystretch",
]


class EquationLengthAssignmentExtractor:
    """
    Extracts length assignments of the form "\\[parameter]=[#][unit of measurement]",
    for example "\\arraycolsep=2pt"
    """

    def parse(self, tex: str) -> Iterator[LengthAssignment]:
        parameter_names_pattern = (
            r"(?:" + "|".join([r"\\" + p for p in ARRAY_PARAMETERS]) + ")"
        )
        unit_pattern = r"(?:" + "|".join(LENGTH_UNITS) + ")"
        assignment_pattern = (
            parameter_names_pattern + r"\s*=\s*[0-9\.]+\s*" + unit_pattern
        )
        pattern = Pattern("length_assignment", assignment_pattern)
        scanner = scan_tex(tex, [pattern])
        for match in scanner:
            yield LengthAssignment(match.start, match.end)


@dataclass(frozen=True)
class Sentence(Entity):
    text: str


@dataclass(frozen=True)
class PlaintextSegment:
    text: str

    transformed: bool
    " Whether the TeX was transformed at all to make this plaintext. "

    tex_start: int
    " Offset of first character in the TeX that corresponds to this plaintext. "

    tex_end: int
    " Offset after the last character in the TeX that corresponds to this plaintext. "


class PlaintextExtractor:
    """
    Extracts plaintext from TeX. It's definitely not perfect: this extracted text will include
    text extracted from many command arguments, because we knew sometimes it would be wanted, and
    other times it wouldn't. Without more sophisticated macro processing, it's not possible to
    tell which arguments would be rendered as text and which wouldn't.
    
    For the anticipated use case of sentence boundary detection, this spurious argument test is
    often okay and won't often influence the detected boundaries. However, for other natural
    language processing tasks, this plaintext extractor may need to be further refined.
    """

    def __init__(self) -> None:

        # Patterns of text that should be replaced with other plaintext.
        self.REPLACE_PATTERNS = {
            Pattern("backslash_newline", r"\\\\"): "\n",
            Pattern("space_macro", r"\\[ ,]"): " ",
            Pattern("tilde", r"~"): " ",
            # See why we use this strange character for equations in the 'parse' method.
            Pattern("math", r"█+"): "[[math]]",
        }

        # Patterns of text the extractor should skip.
        self.SKIP_PATTERNS = [
            # Many patterns below were written with reference to the LaTeX tokenizer in Python's
            # 'doctools' sources at:
            # http://svn.python.org/projects/doctools/converter/converter/tokenizer.py
            Pattern("macro", r"\\[a-zA-Z]+\*?[ \t]*"),
            RIGHT_BRACE,
            LEFT_BRACE,
            Pattern("left_bracket", r"\["),
            Pattern("right_bracket", r"\]"),
            # The following macros are a backslash followed by an ASCII symbol. This pattern was
            # written with reference to the command list at:
            # http://www.public.asu.edu/~rjansen/latexdoc/ltx-2.html
            # Pattern("symbol_macro", r"\\[@=><+'`-]"),
        ]

    def parse(self, tex: str) -> Iterator[PlaintextSegment]:
        """
        Extract plaintext segments from the TeX. Some TeX will be replaced (e.g., "\\\\" with "\n",
        equations with "[[math]]"). Other TeX will be skipped (e.g., macros, braces, and brackets).
        The 'text' property of the returned segments can be appended to form a string of plaintext.
        """
        # All math equations will be replaced in plaintext with the text "[[math]]". However,
        # returned segments also need to be labeled with their character positions from the
        # original TeX. In a first step, equations are detected using EquationExtractor, and then
        # replaced with a Unicode character (█) so that they can be easily detected in a second
        # step, while preserving the equations' character offsets in the TeX.
        tex_without_math = tex
        equation_extractor = EquationExtractor()
        for equation in equation_extractor.parse(tex):
            tex_without_math = (
                tex_without_math[: equation.start]
                + "█" * (equation.end - equation.start)
                + tex_without_math[equation.end :]
            )

        patterns = list(self.REPLACE_PATTERNS.keys()) + self.SKIP_PATTERNS
        scanner = scan_tex(tex_without_math, patterns, include_unmatched=True)

        # Iterate over all TeX. If a token is supposed to be replaced, replace it and yield the
        # span with the replaced text. If it's supposed to be ignored, discard it. Otherwise, yield
        # a new span with the TeX as plaintext.
        for match in scanner:
            if match.pattern in self.SKIP_PATTERNS:
                continue

            transformed = False
            text = match.text
            if match.pattern in self.REPLACE_PATTERNS:
                transformed = True
                text = self.REPLACE_PATTERNS[match.pattern]

            yield PlaintextSegment(
                text=text,
                transformed=transformed,
                tex_start=match.start,
                tex_end=match.end,
            )


class SentenceExtractor:
    """
    Extract plaintext sentences from TeX, with offsets of the characters they correspond to in
    the input TeX strings. The extracted sentences might include some junk TeX, having the same
    limitations as the plaintext produced by PlaintextExtractor.
    """

    def parse(self, tex: str) -> Iterator[Sentence]:
        # Extract plaintext segments from TeX
        plaintext_extractor = PlaintextExtractor()
        plaintext_segments = plaintext_extractor.parse(tex)

        # Build a map from character offsets in the plaintext to TeX offsets. This will let us
        # map from the character offsets of the sentences returned from the sentence boundary
        # detector back to positions in the original TeX.
        plaintext_to_tex_offset_map = {}
        plaintext = ""
        last_segment = None
        for segment in plaintext_segments:
            for i in range(len(segment.text)):
                tex_offset = (
                    (segment.tex_start + i)
                    if not segment.transformed
                    else segment.tex_start
                )
                plaintext_to_tex_offset_map[len(plaintext) + i] = tex_offset

            # While building the map, also create a contiguous plaintext string
            plaintext += segment.text
            last_segment = segment

        if last_segment is not None:
            plaintext_to_tex_offset_map[len(plaintext)] = last_segment.tex_end

        # Segment the plaintext. Return offsets for each setence relative to the TeX input
        segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)
        for sentence in segmenter.segment(plaintext):
            yield Sentence(
                text=sentence.sent,
                start=plaintext_to_tex_offset_map[sentence.start],
                end=plaintext_to_tex_offset_map[sentence.end],
            )


class BeginDocumentExtractor:
    def parse(self, tex: str) -> Optional[BeginDocument]:
        pattern = Pattern("begin_document", r"\\begin{document}")
        scanner = scan_tex(tex, [pattern], include_unmatched=False)
        try:
            match = next(scanner)
            return BeginDocument(match.start, match.end)
        except StopIteration:
            return None


class DocumentclassExtractor:
    def parse(self, tex: str) -> Optional[Documentclass]:
        patterns = [
            Pattern("documentclass", r"\\documentclass"),
            Pattern("optional_arg", r"\[[^\]]*?\]"),
            Pattern("required_arg", r"{[^}]*?}"),
        ]

        match_stage = "start"
        start: int = -1
        required_arg = None

        scanner = scan_tex(tex, patterns, include_unmatched=True)
        for match in scanner:

            # Once we hit a token that's not the document class or argument, return the document
            # class if the required argument has been found; otherwise, abort.
            if match.pattern.name == "UNKNOWN":
                if match_stage == "awaiting-optional-arg":
                    return Documentclass(start, match.start)
                elif not match.text.isspace():
                    break

            elif match_stage == "start":
                if match.pattern.name != "documentclass":
                    return None
                start = match.start
                match_stage = "awaiting-required-arg"

            elif match_stage == "awaiting-required-arg":
                if match.pattern.name == "required_arg":
                    match_stage = "awaiting-optional-arg"
                    required_arg = match

            elif match_stage == "awaiting-optional-arg":
                if match.pattern.name == "optional_arg":
                    end = match.end
                    return Documentclass(start, end)

        if required_arg is not None:
            return Documentclass(start, required_arg.end)
        return None


class BibitemExtractor:
    def __init__(self) -> None:
        self.current_bibitem_label: Optional[str] = None
        self.bibitem_text = ""
        self.nodes_scanned: Set[TexNode] = set()
        self.bibitems: List[Bibitem] = []

    def parse(self, tex: str) -> Iterator[Bibitem]:
        bibitem_pattern = Pattern("bibitem", r"\\bibitem.*?(?=\\bibitem|\n\n|$|\\end{)")
        for bibitem in scan_tex(tex, [bibitem_pattern]):
            try:
                bibitem_soup = parse_soup(bibitem.text)
            except TexSoupParseError:
                continue
            key = self._extract_key(bibitem_soup)
            tokens = self._extract_text(bibitem_soup)
            yield Bibitem(key, tokens)

    def _extract_key(self, bibitem: TexSoup) -> Optional[str]:
        for arg in bibitem[0].args:
            if isinstance(arg, RArg):
                return str(arg.value)
        return None

    def _extract_text(self, bibitem: TexSoup) -> str:
        text = ""
        for content in list(bibitem.contents)[1:]:
            if isinstance(content, TexNode) and content.string is not None:
                text += content.string
            # One common pattern in TeX is to force capitalization for a bibliography entry by
            # surrounding tokens with curly braces. This gets interpreted (incorrectly)
            # by TeXSoup as an RArg. Here, the contents of an RArg are extracted as literal
            # text. A space is appended after the RArg's value because TeXSoup will remove the
            # spaces between what it interprets as RArgs. As only approximate matching will be
            # performed on the text, erroneous insertion of spaces shouldn't be an issue.
            if isinstance(content, RArg):
                text += content.value + " "
            elif isinstance(content, TokenWithPosition):
                text += str(content)
        return _clean_bibitem_text(text)


def _clean_bibitem_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class MacroExtractor:
    """
    Extracts all instances of a macro defined by 'macro_definition'.
    This extractor follows the argument-parsing logic described on p203-4 of the TeXBook.
    """

    def parse(self, tex: str, macro_definition: MacroDefinition) -> Iterator[Macro]:
        parser = self._parse(tex, macro_definition)
        while True:
            try:
                macro = next(parser)
                yield macro
            # Run until the scanner has indicated that the end of input has been reached.
            except EndOfInput:
                return

    def _parse(self, tex: str, macro_definition: MacroDefinition) -> Iterator[Macro]:
        self.scanner = TexScanner(tex)  # pylint: disable=attribute-defined-outside-init
        name_pattern = Pattern("macro", r"\\" + macro_definition.name)

        # This loop will run until the scanner raises an 'EndOfInput' or indicates another error.
        while True:

            # Parse the macro name.
            step = self.scanner.next([name_pattern])
            macro_start = step.match.start
            token_end = step.match.end

            # Parse each of the expected tokens in the parameter string.
            tokens = re.split(r"(#\d+)", macro_definition.parameter_string)
            if tokens[0] == "":
                del tokens[0]
            if len(tokens) >= 1 and tokens[len(tokens) - 1] == "":
                del tokens[len(tokens) - 1]
            for i, token in enumerate(tokens):
                if re.match(r"#\d+", token):
                    if (i == len(tokens) - 1) or (re.match(r"#\d+", tokens[i + 1])):
                        token_end = self._scan_undelimited_parameter()
                    else:
                        token_end = self._scan_delimited_parameter(tokens[i + 1], tex)
                else:
                    token_end = self._scan_delimiter(token)

            # The macros text is the text of the name and all parameters.
            yield Macro(macro_start, token_end, tex[macro_start:token_end])

    def _scan_undelimited_parameter(self) -> int:
        patterns = [LEFT_BRACE, Pattern("nonspace_character", r"\S")]
        step = self.scanner.next(patterns)

        # If a non-space character, match just the first character.
        if step.match.pattern.name == "nonspace_character":
            return step.match.end

        # If the first match is a left-brace, parse until the braces are balanced.
        brace_depth = 1
        brace_patterns = [LEFT_BRACE, RIGHT_BRACE]
        while True:
            step = self.scanner.next(brace_patterns)
            if step.match.pattern.name == "left_brace":
                brace_depth += 1
            elif step.match.pattern.name == "right_brace":
                brace_depth -= 1
            if brace_depth == 0:
                return step.match.end

    def _scan_delimited_parameter(self, delimiter: str, tex: str) -> int:
        scan_start = self.scanner.i

        # Scan for the delimiter with a lookahead so that the scanner doesn't consume the tokens
        # for the delimiter while searching for it.
        delimiter_pattern = Pattern("delimiter", "(?=" + re.escape(delimiter) + ")")

        while True:
            step = self.scanner.next([delimiter_pattern])
            text_before_delimiter = tex[scan_start : step.match.start]
            if has_balanced_braces(text_before_delimiter):
                return step.match.start

    def _scan_delimiter(self, delimiter: str) -> int:
        pattern = Pattern("delimiter", re.escape(delimiter))
        step = self.scanner.next([pattern], include_unmatched=True)
        if step.skipped is not None and len(step.skipped) > 0:
            logging.warning(
                "Unexpectedly found unmatched text before macro argument delimiter."
            )
        return step.match.end


def parse_soup(tex: str) -> TexSoup:
    """
    Use this utility method for parsing TeX fragment into a TexSoup object.
    Only use this for parsing fragments of TeX. Do not use it for parsing full files: TexSoup fails
    on them often enough that your parser will fail on many files. Instead, for processing full TeX
    files, consider using 'scan_tex' which is more bare but much more fault tolerant. You can use it
    to build your own lightweight task-specific parsers like EquationExtractor.
    """
    try:
        soup = TexSoup(tex)
        return soup
    except (TypeError, EOFError) as e:
        raise TexSoupParseError(str(e))


class TexSoupParseError(Exception):
    """
    Error parsing a TeX file using TexSoup.
    """
