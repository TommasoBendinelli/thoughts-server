# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Conservative visible-token expectations for TeX math, without text search."""
from __future__ import annotations
import re
from collections import Counter
import unicodedata
from thoughts_server.proposal_syntax import read_tex_wrapper

from .review_source_staging import COMMAND, group

GREEK = dict(zip(
    'alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega'.split(),
    'αβγδεζηθικλμνξοπρστυφχψω'))
GREEK.update({name.capitalize(): value.upper() for name,value in list(GREEK.items())})
GREEK.update({'varepsilon':'ε','vartheta':'θ','varphi':'φ','varrho':'ρ','varsigma':'σ','varpi':'π','ell':'l','imath':'i','jmath':'j'})
WORDS = set('sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh log ln exp lim limsup liminf max min arg argmax argmin det dim ker Pr mod bmod pmod gcd sup inf'.split())
TRANSPARENT = set('frac dfrac tfrac sqrt binom dbinom tbinom text textbf textrm textit texttt textsf textsc mathrm mathbf mathit mathsf mathtt mathcal mathbb mathbbm mathscr boldsymbol bm operatorname overline underline underbrace overbrace overset underset stackrel xrightarrow xleftarrow substack boxed'.split())
NONPRINTING = set('left right middle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr displaystyle textstyle scriptstyle scriptscriptstyle quad qquad enspace thinspace medspace thickspace negthinspace notag nonumber limits nolimits displaylimits'.split())
SYMBOLS = set('cdot cdots ldots dots vdots ddots times div pm mp sum prod coprod int iint iiint oint infty partial nabla sim simeq approx equiv neq ne le leq ge geq ll gg in notin ni subset subseteq supset supseteq cup cap setminus emptyset varnothing forall exists neg land lor wedge vee to rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow mapsto uparrow downarrow vert Vert mid parallel perp top bot propto ast star circ bullet dagger ddagger check hat bar vec tilde widehat widetilde dot ddot lbrace rbrace langle rangle lvert rvert lVert rVert lceil rceil lfloor rfloor backslash prime'.split())


def math_tokens(value: str) -> Counter | None:
    output=[];at=0
    while at<len(value):
        if value[at]=='%':
            end=value.find('\n',at);at=len(value) if end<0 else end+1;continue
        match=COMMAND.match(value,at)
        if not match:
            output.append(value[at]);at+=1;continue
        name=match[1];at=match.end()
        if name == 'agentProposedEdit':
            # Metadata, ID, explanation and the original are not typeset (shared grammar).
            wrapper = read_tex_wrapper(value, match.start())
            if wrapper is None or len(wrapper.groups) < 3:
                return None
            at = wrapper.groups[2].end
            continue
        if name in {'CommentHighlight', 'CommentStrikethrough', 'CommentAnchor', 'ReviewAnchor'}:
            # IDs and comments are not typeset.
            for _ in range(2):
                arg = group(value, at, len(value))
                if not arg:
                    return None
                at = arg[3]
            continue
        if name in {'begin','end','label','hspace','vspace','color','phantom','hphantom','vphantom'}:
            arg=group(value,at,len(value))
            if not arg:return None
            at=arg[3];continue
        if name=='\\':
            arg=group(value,at,len(value),'[',']')
            if arg:at=arg[3]
            continue
        if name in GREEK:output.append(GREEK[name])
        elif name in WORDS:output.append(name.removeprefix('b').removeprefix('p') if name in {'bmod','pmod'} else name)
        elif name in TRANSPARENT or name in NONPRINTING or name in SYMBOLS or len(name)==1:pass
        else:return None
    clean=''.join(output)
    clean=re.sub(r'\(cid:[0-9]+\)', '', clean)
    return Counter(c for c in unicodedata.normalize('NFKD',clean).casefold() if c.isalnum())


def glyph_math_tokens(value: str) -> Counter:
    value=re.sub(r'\(cid:[0-9]+\)', '', value)
    return Counter(c for c in unicodedata.normalize('NFKD',value).casefold() if c.isalnum())
