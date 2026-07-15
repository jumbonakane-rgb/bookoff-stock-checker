def split_markdown_row(line):
    """Split one Markdown table row while preserving escaped pipe characters."""
    row = line.strip()
    if not row.startswith("|") or not row.endswith("|"):
        raise ValueError("Markdown table row must start and end with a pipe")

    cells = []
    buffer = []
    escaped = False
    for char in row[1:-1]:
        if escaped:
            if char in {"|", "\\"}:
                buffer.append(char)
            else:
                buffer.extend(("\\", char))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)

    if escaped:
        buffer.append("\\")
    cells.append("".join(buffer).strip())
    return cells


def escape_markdown_cell(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|")
