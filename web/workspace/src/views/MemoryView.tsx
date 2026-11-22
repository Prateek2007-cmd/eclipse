import { useState } from "react";
import { api, type Fact, type RagChunk, type RagStatus } from "../api";
import { usePoll } from "../hooks";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyState, ErrorBanner, SectionHeader } from "../components/ui";

/** Memory & RAG view: keyword facts and the local RAG store.
 *
 * Facts are recalled by keyword overlap — there is no list-all
 * endpoint, so this view searches rather than listing. RAG
 * chunks are redacted text only; the store refuses text that
 * still matches a PII pattern. Embeddings are local
 * (all-MiniLM-L6-v2 via ONNX, no cloud, no API key). */
export function MemoryView({ token }: { token: string | null }) {
  const [error, setError] = useState<string | null>(null);

  // Facts
  const [factText, setFactText] = useState("");
  const [factTopic, setFactTopic] = useState("");
  const [factSaved, setFactSaved] = useState<string | null>(null);
  const [savingFact, setSavingFact] = useState(false);
  const [factQuery, setFactQuery] = useState("");
  const [facts, setFacts] = useState<Fact[] | null>(null);
  const [searchingFacts, setSearchingFacts] = useState(false);

  // RAG
  const [ragStatus, setRagStatus] = useState<RagStatus | null>(null);
  const [ragQuery, setRagQuery] = useState("");
  const [ragResults, setRagResults] = useState<RagChunk[] | null>(null);
  const [ragModelAvailable, setRagModelAvailable] = useState<boolean | null>(null);
  const [searchingRag, setSearchingRag] = useState(false);
  const [chunkId, setChunkId] = useState("");
  const [chunkText, setChunkText] = useState("");
  const [chunkKind, setChunkKind] = useState("fact");
  const [indexing, setIndexing] = useState(false);
  const [indexResult, setIndexResult] = useState<string | null>(null);
  const [forgetId, setForgetId] = useState<string | null>(null);
  const [forgetting, setForgetting] = useState(false);

  usePoll(
    async () => {
      try {
        const data = await api.ragStatus();
        setRagStatus(data);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [token],
    30000,
  );

  const saveFact = async () => {
    setSavingFact(true);
    setError(null);
    setFactSaved(null);
    try {
      const data = await api.storeFact(
        factText.trim(),
        "user",
        factTopic.trim() || null,
      );
      setFactSaved(`Fact stored (${data.id}).`);
      setFactText("");
      setFactTopic("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingFact(false);
    }
  };

  const searchFacts = async () => {
    setSearchingFacts(true);
    setError(null);
    try {
      const data = await api.recallFacts(factQuery.trim(), 10);
      setFacts(data.facts);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSearchingFacts(false);
    }
  };

  const searchRag = async () => {
    setSearchingRag(true);
    setError(null);
    try {
      const data = await api.queryRag(ragQuery.trim(), 10);
      setRagResults(data.results);
      setRagModelAvailable(data.model_available);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSearchingRag(false);
    }
  };

  const indexChunk = async () => {
    setIndexing(true);
    setError(null);
    setIndexResult(null);
    try {
      await api.indexChunk(chunkId.trim(), chunkText.trim(), chunkKind.trim() || "fact");
      setIndexResult(`Chunk ${chunkId.trim()} indexed.`);
      setChunkId("");
      setChunkText("");
      const data = await api.ragStatus();
      setRagStatus(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setIndexing(false);
    }
  };

  const forgetChunk = async () => {
    if (!forgetId) return;
    setForgetting(true);
    setError(null);
    try {
      await api.forgetChunk(forgetId);
      setRagResults((prev) => (prev ? prev.filter((c) => c.id !== forgetId) : prev));
      const data = await api.ragStatus();
      setRagStatus(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setForgetting(false);
      setForgetId(null);
    }
  };

  return (
    <div className="view memory-view">
      <div className="view-head">
        <h2>Memory &amp; RAG</h2>
        <p className="muted">
          Two stores: keyword facts the planner recalls, and the local
          embedding store. Both hold redacted text only.
        </p>
      </div>
      {error && <ErrorBanner error={error} />}

      <div className="split">
        <section aria-label="Facts" className="pane">
          <SectionHeader title="Facts" />
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void saveFact();
            }}
          >
            <label htmlFor="fact-text">Remember this</label>
            <textarea
              id="fact-text"
              value={factText}
              onChange={(e) => setFactText(e.target.value)}
              rows={3}
              placeholder="e.g. Prefers remote roles in Hyderabad or Bangalore"
              required
            />
            <label htmlFor="fact-topic">Topic (optional)</label>
            <input
              id="fact-topic"
              value={factTopic}
              onChange={(e) => setFactTopic(e.target.value)}
              placeholder="e.g. preferences"
            />
            <button type="submit" className="primary" disabled={savingFact}>
              {savingFact ? "Storing…" : "Store fact"}
            </button>
            {factSaved && (
              <p role="status" className="success">
                {factSaved}
              </p>
            )}
          </form>
          <h3>Recall</h3>
          <p className="muted">
            Recall is by keyword overlap with your query. There is no
            list-all endpoint, so facts surface only through search.
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void searchFacts();
            }}
          >
            <label htmlFor="fact-query">Search facts</label>
            <input
              id="fact-query"
              value={factQuery}
              onChange={(e) => setFactQuery(e.target.value)}
              required
            />
            <button type="submit" disabled={searchingFacts}>
              {searchingFacts ? "Searching…" : "Search"}
            </button>
          </form>
          {facts === null ? (
            <EmptyState>No search yet.</EmptyState>
          ) : facts.length === 0 ? (
            <EmptyState>No facts match that query.</EmptyState>
          ) : (
            <ul className="plain-list">
              {facts.map((f) => (
                <li key={f.id}>
                  <p>{f.text}</p>
                  <p className="muted">
                    score <span className="mono">{f.score}</span>
                    {f.topic ? ` · topic ${f.topic}` : ""} · {f.source}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section aria-label="RAG store" className="pane">
          <SectionHeader title="RAG store" />
          {ragStatus ? (
            <dl className="kv">
              <dt>Chunks indexed</dt>
              <dd className="mono">{ragStatus.chunks}</dd>
              <dt>Model</dt>
              <dd>
                {ragStatus.model} ({ragStatus.dimensions} dims)
                {ragStatus.model_available ? "" : " — not available"}
              </dd>
              <dt>Relevance threshold</dt>
              <dd className="mono">cosine ≥ {ragStatus.threshold}</dd>
            </dl>
          ) : (
            <p className="muted">Loading RAG status…</p>
          )}
          <h3>Query</h3>
          <p className="muted">
            Only chunks at or above the relevance threshold come back —
            never a weak match.
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void searchRag();
            }}
          >
            <label htmlFor="rag-query">Search the store</label>
            <input
              id="rag-query"
              value={ragQuery}
              onChange={(e) => setRagQuery(e.target.value)}
              required
            />
            <button type="submit" disabled={searchingRag}>
              {searchingRag ? "Searching…" : "Search"}
            </button>
          </form>
          {ragModelAvailable === false && (
            <p role="alert" className="warning">
              The embeddings model is not available on the server
              (server/rag/models/) — queries return nothing until it is.
            </p>
          )}
          {ragResults === null ? (
            <EmptyState>No search yet.</EmptyState>
          ) : ragResults.length === 0 ? (
            <EmptyState>Nothing relevant — no chunk reached the threshold.</EmptyState>
          ) : (
            <ul className="plain-list">
              {ragResults.map((c) => (
                <li key={c.id}>
                  <p>{c.text}</p>
                  <p className="muted">
                    <span className="mono">{c.id}</span> · {c.kind} · score{" "}
                    <span className="mono">{c.score}</span>
                  </p>
                  <button type="button" onClick={() => setForgetId(c.id)}>
                    Forget this chunk
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <section aria-label="Index a chunk" className="pane">
        <SectionHeader title="Index a chunk" />
        <p className="muted">
          Index redacted text only. The store refuses text that still
          matches a PII pattern — but the refusal is regex-only, so names,
          addresses, rolls, CVV, OTP and passwords are NOT caught by the
          store. Redact first.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void indexChunk();
          }}
        >
          <div className="form-grid">
            <div>
              <label htmlFor="chunk-id">Chunk id</label>
              <input
                id="chunk-id"
                value={chunkId}
                onChange={(e) => setChunkId(e.target.value)}
                required
                placeholder="e.g. pref-remote-1"
              />
            </div>
            <div>
              <label htmlFor="chunk-kind">Kind</label>
              <input
                id="chunk-kind"
                value={chunkKind}
                onChange={(e) => setChunkKind(e.target.value)}
              />
            </div>
          </div>
          <label htmlFor="chunk-text">Redacted text</label>
          <textarea
            id="chunk-text"
            value={chunkText}
            onChange={(e) => setChunkText(e.target.value)}
            rows={4}
            required
          />
          <button type="submit" className="primary" disabled={indexing}>
            {indexing ? "Indexing…" : "Index chunk"}
          </button>
          {indexResult && (
            <p role="status" className="success">
              {indexResult}
            </p>
          )}
        </form>
      </section>

      <p className="muted view-foot">
        Stored facts have no delete endpoint — they are recalled by
        keyword and stay until the database is cleared. RAG chunks can
        be forgotten from the search results above.
      </p>

      <ConfirmDialog
        open={forgetId !== null}
        title="Forget this chunk?"
        confirmLabel="Forget chunk"
        danger
        pending={forgetting}
        onConfirm={() => void forgetChunk()}
        onCancel={() => setForgetId(null)}
      >
        <p>
          Chunk <span className="mono">{forgetId}</span> is removed from
          the store and excluded from future retrieval. This cannot be
          undone.
        </p>
      </ConfirmDialog>
    </div>
  );
}
