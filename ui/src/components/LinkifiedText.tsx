import { splitTextWithUrls } from "../browser/urlDetect";

type Props = {
  text: string;
  className?: string;
  onCtrlClickUrl?: (url: string, clientX: number, clientY: number) => void;
};

/** Plain text with http(s) URLs highlighted; Ctrl/Cmd+click offers sandbox open. */
export function LinkifiedText({ text, className, onCtrlClickUrl }: Props) {
  const parts = splitTextWithUrls(text);
  return (
    <pre className={className}>
      {parts.map((p, i) => {
        if (p.type === "text") {
          return <span key={i}>{p.value}</span>;
        }
        return (
          <a
            key={i}
            href={p.value}
            className="sandbox-hot-link"
            title="Ctrl+单击可在浏览器沙盒打开"
            onClick={(e) => {
              if (e.ctrlKey || e.metaKey) {
                e.preventDefault();
                e.stopPropagation();
                onCtrlClickUrl?.(p.value, e.clientX, e.clientY);
                return;
              }
              // Keep default navigation in a new tab for plain click.
            }}
            target="_blank"
            rel="noreferrer"
          >
            {p.value}
          </a>
        );
      })}
    </pre>
  );
}
