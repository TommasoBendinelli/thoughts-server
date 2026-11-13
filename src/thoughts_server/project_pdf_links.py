# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Preserve links across document slices when assembling a Project reading PDF."""
from pypdf.generic import ArrayObject, DictionaryObject, NameObject


def restore_project_links(writer, pages, filenames=None):
    """Map internal destinations after all selected pages have been appended.

    PdfWriter.append filters links whose target is outside that individual slice.
    A Project can include that target in another slice, even in a different order.
    Keep external actions unchanged and leave unselected internal targets inactive.
    """
    filenames = filenames or {}
    readers = {identity: reader for identity, reader, _, _ in pages}
    destinations = {}
    for digest, reader, source_index, output_index in pages:
        page = reader.pages[source_index]
        destinations[digest, source_index] = writer.pages[output_index].indirect_reference
        if page.indirect_reference is not None:
            destinations[digest, page.indirect_reference.idnum, page.indirect_reference.generation] = (
                writer.pages[output_index].indirect_reference
            )

    def destination(digest, reader, value):
        value = value.get_object()
        if isinstance(value, str):
            named = reader.named_destinations.get(value)
            if named is None:
                return None
            value = named.dest_array
        if not isinstance(value, (list, tuple)) or not value:
            return None
        target = value[0]
        if hasattr(target, "idnum"):
            page = destinations.get((digest, target.idnum, target.generation))
        elif isinstance(target, int):
            page = destinations.get((digest, int(target)))
        else:
            page = None
        if page is None:
            return None
        return ArrayObject([page, *(item.clone(writer) for item in value[1:])])

    for digest, reader, source_index, output_index in pages:
        original = reader.pages[source_index]
        output = writer.pages[output_index]
        annotations = ArrayObject([
            item for item in output.get("/Annots", [])
            if item.get_object().get("/Subtype") != "/Link"
        ])
        for item in original.get("/Annots", []):
            source = item.get_object()
            if source.get("/Subtype") != "/Link":
                continue
            link = DictionaryObject({
                key: value.clone(writer) for key, value in source.items()
                if key not in {"/P", "/Dest", "/A"}
            })
            link[NameObject("/P")] = output.indirect_reference
            if "/Dest" in source:
                target = destination(digest, reader, source["/Dest"])
                if target is not None:
                    link[NameObject("/Dest")] = target
            if "/A" in source:
                action = source["/A"].get_object()
                if action.get("/S") == "/GoTo":
                    target = destination(digest, reader, action["/D"]) if "/D" in action else None
                    if target is not None:
                        copied = DictionaryObject({key: value.clone(writer) for key, value in action.items()
                                                   if key != "/D"})
                        copied[NameObject("/D")] = target
                        link[NameObject("/A")] = copied
                elif action.get("/S") == "/GoToR":
                    from pathlib import PurePosixPath
                    remote = action.get("/F", "")
                    if hasattr(remote, "get_object"):
                        remote = remote.get_object()
                    if isinstance(remote, dict):
                        remote = remote.get("/UF", remote.get("/F", ""))
                    identity = filenames.get(PurePosixPath(str(remote)).name)
                    target = destination(identity, readers[identity], action["/D"]) if identity in readers and "/D" in action else None
                    if target is not None:
                        link[NameObject("/A")] = DictionaryObject({NameObject("/S"): NameObject("/GoTo"), NameObject("/D"): target})
                    else:
                        link[NameObject("/A")] = action.clone(writer)
                else:
                    link[NameObject("/A")] = action.clone(writer)
            # add_annotation accepts its builder's destination dictionary, not
            # an already resolved PDF destination array; attach that afterward.
            target = link.pop("/Dest", None)
            inserted = writer.add_annotation(output_index, link)
            if target is not None:
                inserted[NameObject("/Dest")] = target
            annotations.append(inserted.indirect_reference)
        if annotations:
            output[NameObject("/Annots")] = annotations
