# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Expand simple local TeX macros in review spans, with byte provenance.

This is deliberately restricted to ordinary newcommand parameter substitution.
Unknown definitions, optional defaults, catcode changes, and recursive macros
remain unsupported rather than receiving a guessed text placement.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import re


@dataclass
class Expansion:
    raw: bytes
    # Each unchanged/substituted byte interval has its exact authored origin.
    pieces: list[dict]
    replacements: list[dict]

    def offset(self, offset: int, *, end=False) -> int:
        for piece in reversed(self.pieces) if end else self.pieces:
            a,b = piece['generated_start'],piece['generated_end']
            if a <= offset <= b:
                if 'definition_source' in piece:
                    return piece['source_end'] if end else piece['source_start']
                return piece['source_start'] + min(offset-a, piece['source_end']-piece['source_start'])
        raise ValueError('expanded byte has no authored source provenance')


def expand_review_macros(raw: bytes, scanner, *, source_id='', sources=None,
                        definitions=None) -> Expansion | None:
    from .review_source_staging import COMMAND, group
    from .review_macro_definitions import macro_definitions
    text = raw.decode()
    replacements = []
    for a,b,reason in scanner.uncovered:
        if not reason.startswith('opaque command '): continue
        name = reason.removeprefix('opaque command ')
        # Definition lookup scans the source prefix and loaded styles. Do not
        # pay that cost for commands outside every local or inherited target.
        if not any(start <= a < end or a <= start < end <= b
                   for _key,start,end,_macro in scanner.targets): continue
        definition = macro_definitions(raw, source_id, sources or {}, a, definitions).get(name)
        if definition is None: continue
        count, body = definition.count, definition.body
        match = COMMAND.match(text,a); cursor = match.end()
        args=[]
        for _ in range(count):
            arg=group(text,cursor,len(text))
            if not arg: break
            args.append(arg);cursor=arg[3]
        if len(args)!=count: continue
        # Use one token buffer so argument bytes and definition bytes retain
        # distinct origins, including fixed labels produced by the style file.
        external = definition.source != source_id
        token_text = text + definition.text if external else text
        shift = len(text) if external else 0
        values = _macro_tokens(token_text, body[1] + shift, body[2] + shift, args)
        replacements.append((a, cursor, values, definition if external else None))
    if not replacements: return None
    output=[];pieces=[];changes=[];written=0;cursor=0
    def append(a,b,generated=None):
        nonlocal written
        chunk=text[a:b].encode() if generated is None else generated.encode()
        output.append(chunk)
        if generated is None:
            pieces.append({'generated_start':written,'generated_end':written+len(chunk),
                           'source_start':len(text[:a].encode()),'source_end':len(text[:b].encode())})
        written+=len(chunk)
    for a,b,values,definition in sorted(replacements):
        if a<cursor: continue
        append(cursor,a);start=written
        # A macro argument retains token boundaries without adding an executed
        # TeX group. Its closing brace also preserves the caller's input state.
        append(a,a,r'\thoughtsReviewExpanded{')
        for x, y, literal in values:
            if definition is not None and x >= len(text):
                x -= len(text); y -= len(text)
                first = written
                chunk = definition.text[x:y] if literal is None else literal
                append(a, a, chunk)
                if literal is None:
                    pieces.append({'generated_start': first, 'generated_end': written,
                        'source_start': len(text[:a].encode()), 'source_end': len(text[:b].encode()),
                        'definition_source': definition.source,
                        'definition_start': len(definition.text[:x].encode()),
                        'definition_end': len(definition.text[:y].encode()),
                        'definition_sha256': hashlib.sha256(definition.text.encode()).hexdigest()})
            else:
                append(x, y, literal)
        append(b,b,'}')
        changes.append({'generated_start':start,'generated_end':written,'source_start':len(text[:a].encode()),
                        'source_end':len(text[:b].encode()),'original':text[a:b],'reason':'loaded local macro token expansion' if definition else 'local macro token expansion'})
        cursor=b
    append(cursor,len(text))
    return Expansion(b''.join(output),pieces,changes)


def _macro_tokens(text, start, end, arguments=()):
    """Preserve TeX's space and paragraph tokens across parameter substitution.

    Concatenating raw argument strings changes newline/control-word whitespace,
    and can change minipage spacing or pagination. Serialize those tokens
    explicitly while leaving every non-whitespace source byte attributable.
    """
    from .review_source_staging import COMMAND
    result=[];at=start;state='mid'
    while at < end:
        c=text[at]
        if c=='%':
            edge=text.find('\n',at,end)
            at=end if edge<0 else edge+1
            state='new'
            continue
        if c in ' \t\r':
            if state=='mid': result.append((at,at+1,r'\space '))
            if state!='new':state='skip'
            at+=1;continue
        if c=='\n':
            if state=='mid':result.append((at,at+1,r'\space '))
            elif state=='new':result.append((at,at+1,r'\par '))
            state='new';at+=1;continue
        if c=='#' and arguments and at+1<end and text[at+1].isdigit():
            arg=arguments[int(text[at+1])-1]
            result.extend(_macro_tokens(text,arg[1],arg[2]))
            state='mid';at+=2;continue
        if c=='\\':
            match=COMMAND.match(text,at)
            if match:
                result.append((at,match.end(),None))
                if match[1][0].isalpha() or match[1][0]=='@':
                    result.append((match.end(),match.end(),' '))
                    state='skip'
                else:state='mid'
                at=match.end();continue
        result.append((at,at+1,None));at+=1;state='mid'
    return result


def remap_report(original, expansion, generated, report, original_targets, source_id):
    """Compose exact byte intervals back to the authored file after expansion."""
    import hashlib
    intervals = []
    for span in report['segments']:
        for piece in expansion.pieces:
            if 'definition_source' in piece:
                continue
            a = max(span['source_start'], piece['generated_start'])
            b = min(span['source_end'], piece['generated_end'])
            if a >= b:
                continue
            g = span['generated_start'] + a - span['source_start']
            o = piece['source_start'] + a - piece['generated_start']
            intervals.append({'generated_start': g, 'generated_end': g+b-a,
                              'source_start': o, 'source_end': o+b-a})
    report['segments'] = intervals
    for marker in report['markers']:
        if not marker.get('source_file') or marker['source_file'] == source_id:
            marker['source_offset'] = expansion.offset(marker['source_offset'])
        if marker['key'] in original_targets:
            marker['target_start'], marker['target_end'] = original_targets[marker['key']]
        else:
            marker['target_start'] = expansion.offset(marker['target_start'])
            marker['target_end'] = expansion.offset(marker['target_end'], end=True)
    for span in report['render_segments']:
        if span.get('source_file') and span['source_file'] != source_id:
            continue
        a, b = span['source_start'], span['source_end']
        span['source_text'] = span.get('source_text', expansion.raw[a:b].decode())
        origins = []
        definition_origins = []
        calls = []
        for piece in expansion.pieces:
            first, last = max(a, piece['generated_start']), min(b, piece['generated_end'])
            if first < last:
                if 'definition_source' in piece:
                    definition_origins.append({'source_file': piece['definition_source'],
                        'source_start': piece['definition_start'] + first - piece['generated_start'],
                        'source_end': piece['definition_start'] + last - piece['generated_start'],
                        'source_sha256': piece['definition_sha256']})
                    calls.append([piece['source_start'], piece['source_end']])
                    continue
                start = piece['source_start'] + first-piece['generated_start']
                origins.append([start, start+last-first])
        span['source_intervals'] = origins
        if definition_origins:
            span['definition_origins'] = definition_origins
        if origins or calls:
            span['source_start'] = min(x[0] for x in origins + calls)
            span['source_end'] = max(x[1] for x in origins + calls)
        span['source_sha256'] = hashlib.sha256(original).hexdigest()
    report['macro_expansions'] = expansion.replacements
    report['source_sha256'] = hashlib.sha256(original).hexdigest()
    # SyncTeX line numbers refer to the shadow file. Attribute each expanded
    # line to its first actual authored token, including repeated arguments.
    lines = []
    cursor = 0
    previous = 1
    for line in generated.splitlines(keepends=True):
        edge = cursor + len(line)
        for span in intervals:
            if span['generated_end'] > cursor and span['generated_start'] < edge:
                start = max(cursor, span['generated_start'])
                authored = span['source_start'] + start-span['generated_start']
                previous = original[:authored].count(b'\n') + 1
                break
        lines.append(previous)
        cursor = edge
    report['source_line_map'] = lines
    return report
