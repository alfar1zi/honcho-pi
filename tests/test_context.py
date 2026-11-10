

def test_context_block_caps_lines():
    from omp_memory import context

    body = "\n".join(f"line {i}" for i in range(40))
    block = context.build_memory_context_block(body)
    lines = block.splitlines()
    assert len(lines) <= context.MAX_CONTEXT_LINES + 4  # fences + note + omission marker
    assert "more recall lines omitted" in block
    assert "line 39" not in block
