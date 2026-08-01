import type { FileDiffPreview } from "../utils/diffPreview";

type Props = {
  diff: FileDiffPreview | null;
  loading?: boolean;
  title?: string;
  newFileLabel?: string;
  truncatedLabel?: string;
  emptyLabel?: string;
  alreadyAppliedLabel?: string;
  snippetLabel?: string;
  compact?: boolean;
};

/** Unified line diff for write_file / str_replace review. */
export function DiffReview({
  diff,
  loading,
  title = "变更预览",
  newFileLabel = "新建文件",
  truncatedLabel = "已截断显示",
  emptyLabel = "无文本变更",
  alreadyAppliedLabel = "已应用到文件",
  snippetLabel = "片段对比",
  compact,
}: Props) {
  if (loading) {
    return (
      <div className={`diff-review${compact ? " compact" : ""}`}>
        <div className="diff-review-head">
          <strong>{title}</strong>
        </div>
        <div className="diff-review-empty">…</div>
      </div>
    );
  }
  if (!diff) {
    return (
      <div className={`diff-review${compact ? " compact" : ""}`}>
        <div className="diff-review-empty">{emptyLabel}</div>
      </div>
    );
  }

  const adds = diff.statAdd ?? diff.lines.filter((l) => l.kind === "add").length;
  const dels = diff.statDel ?? diff.lines.filter((l) => l.kind === "del").length;
  const hasChanges = adds > 0 || dels > 0 || diff.lines.some((l) => l.kind === "add" || l.kind === "del");

  return (
    <div className={`diff-review${compact ? " compact" : ""}`}>
      <div className="diff-review-head">
        <strong>{title}</strong>
        <span className="diff-review-path" title={diff.path}>
          {diff.path}
        </span>
        <span className="diff-review-meta">
          {diff.isNew ? <span className="diff-badge">{newFileLabel}</span> : null}
          {diff.snippetOnly ? <span className="diff-badge">{snippetLabel}</span> : null}
          {diff.alreadyApplied ? (
            <span className="diff-badge">{alreadyAppliedLabel}</span>
          ) : null}
          {hasChanges || adds > 0 || dels > 0 ? (
            <span className="diff-stats">
              <span className="diff-stat del">−{dels}</span>
              <span className="diff-stat add">+{adds}</span>
            </span>
          ) : (
            <span className="diff-stat muted">{emptyLabel}</span>
          )}
          {diff.truncated ? <span className="diff-trunc">{truncatedLabel}</span> : null}
        </span>
      </div>
      <div className="diff-review-body" role="table" aria-label={title}>
        {!hasChanges ? (
          <div className="diff-review-empty">{emptyLabel}</div>
        ) : (
          diff.lines.map((line, i) => {
            if (line.kind === "skip") {
              return (
                <div key={i} className="diff-line skip">
                  <span className="diff-ln" />
                  <span className="diff-sign" />
                  <span className="diff-text">{line.text}</span>
                </div>
              );
            }
            const ln =
              line.kind === "del"
                ? line.oldNo
                : line.kind === "add"
                  ? line.newNo
                  : (line.newNo ?? line.oldNo);
            return (
              <div key={i} className={`diff-line ${line.kind}`}>
                <span className="diff-ln">{ln ?? ""}</span>
                <span className="diff-sign" aria-hidden>
                  {line.kind === "add" ? "+" : line.kind === "del" ? "−" : " "}
                </span>
                <span className="diff-text">{line.text || " "}</span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
