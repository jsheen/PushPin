"""pushpin-profile: LLM themes of what each reader clicks and what they skip.

For each day D from split.fit_end on, every user with an impression that day gets one profile
built only from data before D, so it never sees the impressions it is used to score:

  retrieve  their most recent clicks (pre-period history + clicks in earlier impressions) and,
            for each, the skipped articles closest to it in abstract-embedding space, searched
            within the user's own earlier impressions (shown, not clicked, and not clicked since)
  generate  Claude names 2-4 click_themes and 2-4 skip_themes (Message Batches API, structured output)
  embed     each theme on its own, with the same sentence model as the abstracts, into Chroma

One vector per theme rather than one per summary: a reader of football, diets and politics gets
three sharp vectors instead of one blurred average. features.llm_features scores each candidate
by its best-matching theme, for the profile of its day.
Outputs under data/<profile.dir>/: profiles.parquet, theme_vectors_{click,skip}.npy (the padded
theme vectors, read memory-mapped at training time instead of through Chroma), and batches.json
(the submitted batch ids, so a rerun resumes the same batches instead of paying twice).
"""

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from pushpin.data import explode_impressions, load_behaviors, load_news
from pushpin.vectordb import ArticleStore

SYSTEM = """You name the themes in two sets of news articles: articles a reader clicked and \
articles they were shown but skipped.

Return:
- click_themes: the 2-4 dominant themes of the clicked articles.
- skip_themes: the 2-4 dominant themes of the skipped articles.

A theme groups several articles. Write each as a short phrase in the style of a news headline or \
section topic, for example "NFL game recaps, injuries and trade rumors" or "Healthy Thanksgiving \
recipes and side dishes". Do not list individual articles, and ignore one-off stories that fit no \
theme. A theme can be a kind of item, such as "Celebrity photo galleries" or "NFL video \
highlights", when that is what the articles have in common.

Each set's themes describe only that set. Never mention, compare with, or contrast against the \
other set, and do not use words like "rather than", "unlike" or "instead of". Describe the \
articles, not the reader. Use only what the listed articles contain. If no skipped articles are \
listed, return an empty skip_themes list."""

SCHEMA = {
    "type": "object",
    "properties": {"click_themes": {"type": "array", "items": {"type": "string"}},
                   "skip_themes": {"type": "array", "items": {"type": "string"}}},
    "required": ["click_themes", "skip_themes"],
    "additionalProperties": False,
}
PRICE = {"claude-haiku-5-5": (0.10, 0.50)}  # $ per 1M input / output tokens; batches are half
BATCH_SIZE = 10_000  # requests per batch (the API allows up to 100,000)
MAX_THEMES = 4  # the prompt asks for 2-4; extra themes past this are dropped
KINDS = ("click", "skip")


def snapshot_days(beh: pd.DataFrame, fit_end: pd.Timestamp) -> list[pd.Timestamp]:
    """One snapshot per day with impressions at or after fit_end (the train and test days)."""
    return sorted(pd.Timestamp(d) for d in beh.loc[beh["time"] >= fit_end, "time"].dt.normalize().unique())


def retrieve(beh: pd.DataFrame, news_idx: dict[str, int], vec: np.ndarray, days: list[pd.Timestamp],
             max_clicks: int, skips_per_click: int) -> pd.DataFrame:
    """Per (day, user active that day): recent clicks and the nearest skipped articles to each.

    Only impressions before the day are used. Users with no clicks at all get no profile.
    """
    rows = explode_impressions(beh, news_idx)
    rows["user_id"] = beh["user_id"].to_numpy()[rows["row"]]
    rows["time"] = beh["time"].to_numpy()[rows["row"]]
    history = beh.drop_duplicates("user_id").set_index("user_id")["history"]
    out = []
    for i, day in enumerate(days):
        in_day = beh["time"] >= day
        if i + 1 < len(days):
            in_day &= beh["time"] < days[i + 1]
        active = beh.loc[in_day, "user_id"].unique()
        prior = rows[(rows["time"] < day) & rows["user_id"].isin(active)]
        by_user = dict(tuple(prior.groupby("user_id", sort=False)))
        for user in active:
            clicked = [news_idx[n] for n in history[user] if n in news_idx]  # MIND history is oldest first
            skipped = []
            if user in by_user:
                p = by_user[user]
                clicked += p.loc[p["label"] == 1, "news"].tolist()
                skipped = p.loc[p["label"] == 0, "news"].tolist()
            if not clicked:
                continue
            recent = list(dict.fromkeys(reversed(clicked)))[:max_clicks][::-1]  # unique, oldest first
            pool = np.array(sorted(set(skipped) - set(clicked)), dtype=np.int64)
            if len(pool):
                sims = vec[recent] @ vec[pool].T
                near = [pool[j].tolist() for j in np.argsort(-sims, axis=1)[:, :skips_per_click]]
            else:
                near = [[] for _ in recent]
            out.append({"day": day, "user_id": user, "clicks": recent, "near_skips": near})
    df = pd.DataFrame(out, columns=["day", "user_id", "clicks", "near_skips"])
    df["custom_id"] = df["day"].dt.strftime("%Y%m%d") + "-" + df["user_id"]
    return df


def article_text(news: pd.DataFrame, max_words: int) -> list[str]:
    """One line per article for the prompt: (category / subcategory) title - truncated abstract."""
    abstract = news["abstract"].fillna("").str.split().str[:max_words].str.join(" ")
    head = "(" + news["category"] + " / " + news["subcategory"] + ") " + news["title"].fillna("")
    return np.where(abstract != "", head + " - " + abstract, head).tolist()


def build_prompt(clicks: list[int], near_skips: list[list[int]], text: list[str]) -> str:
    lines = ["Clicked articles, oldest first. Under each are the skipped articles most similar to it:"]
    seen = set()
    for c, skips in zip(clicks, near_skips):
        lines.append(f"- CLICKED: {text[c]}")
        for s in skips:
            if s not in seen:
                seen.add(s)
                lines.append(f"    - SKIPPED: {text[s]}")
    if not seen:
        lines.append("(No skipped articles.)")
    return "\n".join(lines)


def request_params(prompt: str, cfg: dict) -> dict:
    return {
        "model": cfg["model"],
        "max_tokens": cfg["max_tokens"],
        "system": SYSTEM,
        "thinking": {"type": "disabled"},  # a short summary; thinking tokens would only add cost
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": prompt}],
    }


def prompt_hash(cfg: dict) -> str:
    """Changes whenever anything that shapes the output changes, so stale profiles are not reused."""
    keys = ("model", "max_clicks", "skips_per_click", "max_words", "max_tokens")
    blob = json.dumps({"system": SYSTEM, "schema": SCHEMA, **{k: cfg[k] for k in keys}}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def run_batches(client, requests: dict[str, dict], state_path: Path, version: str,
                poll_seconds: int = 60) -> tuple[dict[str, dict], dict]:
    """Submit (or resume) the batches, wait for them, and parse each summary.

    Returns {custom_id: {"click_themes", "skip_themes"}} for the successes and a report with
    token usage and failures. batches.json records submitted batch ids per prompt version.
    """
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get("version") != version:
        state = {"version": version, "batch_ids": []}
    ids = sorted(requests)
    for start in range(len(state["batch_ids"]) * BATCH_SIZE, len(ids), BATCH_SIZE):
        chunk = ids[start:start + BATCH_SIZE]
        batch = client.messages.batches.create(
            requests=[{"custom_id": c, "params": requests[c]} for c in chunk])
        state["batch_ids"].append(batch.id)
        state_path.write_text(json.dumps(state, indent=2))
        print(f"submitted batch {batch.id} ({len(chunk):,} requests)")

    summaries, failed = {}, {}
    usage = {"input_tokens": 0, "output_tokens": 0}
    for batch_id in state["batch_ids"]:
        while (batch := client.messages.batches.retrieve(batch_id)).processing_status != "ended":
            print(f"{batch_id}: {batch.processing_status}, {batch.request_counts.processing:,} processing")
            time.sleep(poll_seconds)
        for res in client.messages.batches.results(batch_id):
            if res.result.type != "succeeded":
                failed[res.custom_id] = res.result.type
                continue
            msg = res.result.message
            usage["input_tokens"] += msg.usage.input_tokens
            usage["output_tokens"] += msg.usage.output_tokens
            if msg.stop_reason != "end_turn":  # refusal or max_tokens: no complete JSON
                failed[res.custom_id] = msg.stop_reason
                continue
            text = next((b.text for b in msg.content if b.type == "text"), "")
            try:
                summaries[res.custom_id] = json.loads(text)
            except json.JSONDecodeError:
                failed[res.custom_id] = "invalid_json"
    return summaries, {"usage": usage, "failed": failed}


def cost(model: str, input_tokens: float, output_tokens: float) -> float | None:
    """Batch-priced dollars, or None for a model without a listed price."""
    if model not in PRICE:
        return None
    price_in, price_out = PRICE[model]
    return 0.5 * (input_tokens * price_in + output_tokens * price_out) / 1e6


def clean_themes(themes: list) -> list[str]:
    """Stripped, non-empty, de-duplicated, at most MAX_THEMES."""
    return list(dict.fromkeys(t.strip() for t in themes if isinstance(t, str) and t.strip()))[:MAX_THEMES]


def theme_rows(profiles: pd.DataFrame) -> pd.DataFrame:
    """One row per (profile, kind, theme): the Chroma id '<custom_id>-<kind>-<j>' and its text."""
    out = []
    for kind in KINDS:
        part = profiles[["custom_id", "user_id", "day", f"{kind}_themes"]].explode(f"{kind}_themes").dropna()
        part["j"] = part.groupby("custom_id").cumcount()
        part["id"] = part["custom_id"] + f"-{kind}-" + part["j"].astype(str)
        out.append(part.rename(columns={f"{kind}_themes": "text"}).assign(kind=kind))
    return pd.concat(out, ignore_index=True)


def embed_profiles(profiles: pd.DataFrame, data_dir: Path, config: dict) -> None:
    """Embed every theme into its own Chroma collection, ids '<custom_id>-<kind>-<j>'."""
    from sentence_transformers import SentenceTransformer  # heavy; only this step needs it

    emb = config["embeddings"]
    model = SentenceTransformer(emb["model"], cache_folder=str(data_dir / "models"))
    store = ArticleStore(data_dir / emb["chroma_dir"], config["profile"]["collection"], reset=True)
    rows = theme_rows(profiles)
    vectors = model.encode(rows["text"].tolist(), batch_size=emb["batch_size"], normalize_embeddings=True,
                           convert_to_numpy=True, show_progress_bar=True)
    meta = [{"user_id": u, "day": d.strftime("%Y-%m-%d"), "kind": k}
            for u, d, k in zip(rows["user_id"], rows["day"], rows["kind"])]
    store.add(rows["id"].tolist(), vectors, rows["text"].tolist(), meta)
    save_theme_vectors(data_dir / config["profile"]["dir"], padded_themes(profiles, rows, [(0, vectors)]))


def padded_themes(profiles: pd.DataFrame, rows: pd.DataFrame, blocks) -> list[np.ndarray]:
    """One (n_profiles, MAX_THEMES, dim) array per kind, NaN-padded, from (start, block) pairs of
    theme vectors in `rows` order. Each block is copied straight into place."""
    pos = pd.Series(np.arange(len(profiles)), index=profiles["custom_id"])[rows["custom_id"]].to_numpy()
    kind_idx = np.asarray([KINDS.index(k) for k in rows["kind"]])
    j = rows["j"].to_numpy()
    out = None
    for s, block in blocks:
        if out is None:
            out = [np.full((len(profiles), MAX_THEMES, block.shape[1]), np.nan, np.float32) for _ in KINDS]
        sl = slice(s, s + len(block))
        for k in range(len(KINDS)):
            m = kind_idx[sl] == k
            out[k][pos[sl][m], j[sl][m]] = block[m]
    if out is None:
        out = [np.full((len(profiles), MAX_THEMES, 1), np.nan, np.float32) for _ in KINDS]
    return out


def theme_paths(out_dir: Path) -> list[Path]:
    return [out_dir / f"theme_vectors_{kind}.npy" for kind in KINDS]


def save_theme_vectors(out_dir: Path, arrays: list[np.ndarray]) -> None:
    for path, arr in zip(theme_paths(out_dir), arrays):
        tmp = path.with_suffix(".tmp.npy")
        np.save(tmp, arr)
        tmp.replace(path)


def load_profiles(data_dir: Path, config: dict) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """(keys with day and user_id, click theme vectors, skip theme vectors).

    Theme vectors are (n_profiles, MAX_THEMES, dim), NaN-padded where a profile has fewer themes.
    They are read memory-mapped from theme_vectors_*.npy: pages are loaded from disk as they are
    used and can be dropped again under memory pressure, and Chroma is never opened. If the files
    are missing or older than profiles.parquet, they are rebuilt from Chroma first.
    """
    pcfg = config["profile"]
    out_dir = data_dir / pcfg["dir"]
    path = out_dir / "profiles.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run pushpin-profile first")
    profiles = pd.read_parquet(path)
    paths = theme_paths(out_dir)
    fresh = all(p.exists() and p.stat().st_mtime >= path.stat().st_mtime for p in paths)
    arrays = [np.load(p, mmap_mode="r") for p in paths] if fresh else None
    if arrays is None or any(len(a) != len(profiles) for a in arrays):
        print("caching theme vectors from Chroma")
        store = ArticleStore(data_dir / config["embeddings"]["chroma_dir"], pcfg["collection"])
        rows = theme_rows(profiles)
        save_theme_vectors(out_dir, padded_themes(profiles, rows, store.iter_vectors(rows["id"].tolist())))
        arrays = [np.load(p, mmap_mode="r") for p in paths]
    return profiles[["day", "user_id"]], arrays[0], arrays[1]


def profile(config: dict, sample: int | None = None, estimate: bool = False) -> None:
    data_dir = Path(config["data"]["dir"])
    pcfg = config["profile"]
    out_dir = data_dir / pcfg["dir"]
    out_dir.mkdir(parents=True, exist_ok=True)

    print("loading data")
    beh = load_behaviors(data_dir / config["data"]["behaviors"])
    news = load_news(data_dir / config["data"]["news"])
    news_idx = dict(zip(news["news_id"], range(len(news))))
    store = ArticleStore(data_dir / config["embeddings"]["chroma_dir"], config["embeddings"]["collection"])
    vec = store.vectors(news["news_id"].tolist())

    days = snapshot_days(beh, pd.Timestamp(config["split"]["fit_end"]))
    print(f"retrieving clicks and nearest skips for {len(days)} day(s): "
          + ", ".join(d.strftime("%Y-%m-%d") for d in days))
    inputs = retrieve(beh, news_idx, vec, days, pcfg["max_clicks"], pcfg["skips_per_click"])
    if sample:
        inputs = inputs.sample(min(sample, len(inputs)), random_state=config["seed"])
    text = article_text(news, pcfg["max_words"])
    requests = {cid: request_params(build_prompt(c, n, text), pcfg)
                for cid, c, n in zip(inputs["custom_id"], inputs["clicks"], inputs["near_skips"])}
    print(f"{len(requests):,} profiles to generate with {pcfg['model']}")

    import anthropic  # only this step calls the API

    client = anthropic.Anthropic()
    if estimate:
        ids = pd.Series(sorted(requests)).sample(min(100, len(requests)), random_state=0)
        counts = [client.messages.count_tokens(model=pcfg["model"], system=SYSTEM,
                                               messages=requests[c]["messages"]).input_tokens for c in ids]
        n_in = float(np.mean(counts)) * len(requests)
        n_out = 200.0 * len(requests)  # two short theme lists
        dollars = cost(pcfg["model"], n_in, n_out)
        print(f"~{n_in / 1e6:.1f}M input tokens (mean {np.mean(counts):.0f} per request), "
              f"~{n_out / 1e6:.1f}M output tokens (assumed 200 per request)")
        print(f"estimated batch cost: ${dollars:.2f}" if dollars is not None else "no price listed for this model")
        (out_dir / "prompt_sample.txt").write_text("\n\n=====\n\n".join(
            requests[c]["messages"][0]["content"] for c in ids[:5]))
        print(f"wrote 5 example prompts to {out_dir / 'prompt_sample.txt'}")
        return

    version = prompt_hash(pcfg) + (f"-sample{sample}" if sample else "")
    summaries, report = run_batches(client, requests, out_dir / "batches.json", version)
    dollars = cost(pcfg["model"], **report["usage"])
    print(f"{len(summaries):,} succeeded, {len(report['failed']):,} failed; "
          f"{report['usage']['input_tokens']:,} input and {report['usage']['output_tokens']:,} output tokens"
          + (f", ${dollars:.2f}" if dollars is not None else ""))
    if report["failed"]:
        print("failures by type:", pd.Series(report["failed"]).value_counts().to_dict())

    done = inputs[inputs["custom_id"].isin(summaries)].copy()
    for kind in KINDS:
        done[f"{kind}_themes"] = [clean_themes(summaries[c].get(f"{kind}_themes", [])) for c in done["custom_id"]]
    done["model"], done["version"] = pcfg["model"], version
    profiles = done[["day", "user_id", "custom_id", "click_themes", "skip_themes", "model", "version"]]
    profiles.to_parquet(out_dir / "profiles.parquet", index=False)
    (out_dir / "report.json").write_text(json.dumps({**report, "cost_usd": dollars}, indent=2))
    print("embedding themes")
    embed_profiles(profiles, data_dir, config)
    print(f"wrote {len(profiles):,} profiles to {out_dir / 'profiles.parquet'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/train.yml")
    parser.add_argument("--sample", type=int, help="only this many (day, user) profiles, for a trial run")
    parser.add_argument("--estimate", action="store_true",
                        help="count tokens on 100 prompts and print the batch cost; submits nothing")
    args = parser.parse_args()
    with open(args.config) as f:
        config = yaml.safe_load(f)
    profile(config, args.sample, args.estimate)


if __name__ == "__main__":
    main()
