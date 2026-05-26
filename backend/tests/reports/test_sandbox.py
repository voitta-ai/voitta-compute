import asyncio

from app.reports import sandbox


def test_smoke_test_runs_build(scripts_root):
    res = sandbox.smoke_test("ok", "def build(ctx):\n    ctx.text('hi')\n    return 42\n")
    assert res.ok
    assert res.result == 42
    assert res.ctx is not None
    assert res.ctx.inline[0].kind == "text"


def test_smoke_test_syntax_error_is_clean():
    res = sandbox.smoke_test("bad", "def build(ctx)\n  pass\n")
    assert not res.ok
    assert "SyntaxError" in res.error
    assert res.traceback


def test_smoke_test_runtime_error_is_clean():
    res = sandbox.smoke_test(
        "boom",
        "def build(ctx):\n    raise ValueError('nope')\n",
    )
    assert not res.ok
    assert "ValueError: nope" in res.error
    assert "Traceback" in (res.traceback or "")


def test_smoke_test_requires_build_function():
    res = sandbox.smoke_test("no_build", "x = 1\n")
    assert not res.ok
    assert "build" in res.error


def test_smoke_test_top_level_error_caught():
    res = sandbox.smoke_test(
        "imp",
        "import this_module_does_not_exist_xyz\n\ndef build(ctx): pass\n",
    )
    assert not res.ok
    assert "ModuleNotFoundError" in res.error or "ImportError" in res.error


def test_ctx_isolation_between_runs():
    """A second smoke_test call must NOT see the first ctx's side-effects."""
    code = "def build(ctx):\n    ctx.text('a')\n    return None\n"
    r1 = sandbox.smoke_test("a", code)
    r2 = sandbox.smoke_test("a", code)
    assert r1.ctx is not r2.ctx
    assert len(r1.ctx.inline) == 1
    assert len(r2.ctx.inline) == 1


def test_run_passes_args():
    res = asyncio.run(sandbox.run(
        "with_args",
        "def build(ctx):\n    return ctx.args['n'] * 2\n",
        args={"n": 21},
    ))
    assert res.ok and res.result == 42
