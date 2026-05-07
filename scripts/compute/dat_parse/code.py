
def run(ctx, args=None):
    handle = (args or {}).get("snapshot", "py_7dbfe230")
    snap = ctx.snapshot(handle)
    df = ctx.dataframe(handle)
    raw = ctx.raw(handle)

    ctx.text(f"**snapshot**: `{handle}` — {snap.get('name')} ({snap.get('bytes'):,} B)")

    info = {
        "df_shape": df.shape if df is not None else None,
        "df_cols": list(df.columns)[:40] if df is not None else None,
        "df_dtypes": {c: str(df[c].dtype) for c in (df.columns[:40] if df is not None else [])},
        "raw_keys": list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__,
    }
    if df is not None and len(df):
        info["df_head"] = df.head(3).to_dict(orient="list")
    return info
