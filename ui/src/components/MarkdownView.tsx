import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import mermaid from "mermaid";
import "highlight.js/styles/github.css";
import { splitTextWithUrls } from "../browser/urlDetect";

mermaid.initialize({
  startOnLoad: false,
  securityLevel: "loose",
  theme: "neutral",
  fontFamily: "Sora, system-ui, sans-serif",
});

type Props = {
  content: string;
  streaming?: boolean;
  /** Ctrl/Cmd+click an http(s) link → ask to open in browser sandbox. */
  onCtrlClickUrl?: (url: string, clientX: number, clientY: number) => void;
};

function nodeText(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (typeof node === "object" && "props" in node) {
    return nodeText((node as { props?: { children?: ReactNode } }).props?.children);
  }
  return "";
}

function MermaidBlock({ chart, streaming }: { chart: string; streaming?: boolean }) {
  const host = useRef<HTMLDivElement>(null);
  const reactId = useId().replace(/:/g, "");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (streaming) return;
    const src = chart.trim();
    if (!src || !host.current) return;
    let cancelled = false;
    const id = `mmd_${reactId}_${Math.random().toString(36).slice(2, 8)}`;
    setErr(null);
    host.current.innerHTML = "";
    void (async () => {
      try {
        const { svg } = await mermaid.render(id, src);
        if (!cancelled && host.current) host.current.innerHTML = svg;
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [chart, streaming, reactId]);

  if (streaming) {
    return (
      <pre className="code-fence mermaid-pending">
        <code>{chart}</code>
      </pre>
    );
  }
  if (err) {
    return (
      <div className="mermaid-error">
        <div className="mermaid-error-label">Mermaid 渲染失败</div>
        <pre className="code-fence">
          <code>{chart}</code>
        </pre>
      </div>
    );
  }
  return <div className="mermaid-block" ref={host} />;
}

function CodeBlock({
  className,
  children,
  streaming,
  onCtrlClickUrl,
}: {
  className?: string;
  children?: ReactNode;
  streaming?: boolean;
  onCtrlClickUrl?: (url: string, clientX: number, clientY: number) => void;
}) {
  const text = nodeText(children).replace(/\n$/, "");
  const lang = /language-([\w-]+)/.exec(className || "")?.[1] || "";
  if (lang === "mermaid") {
    return <MermaidBlock chart={text} streaming={streaming} />;
  }
  // Shell / plain logs: make bare URLs Ctrl+clickable for sandbox open.
  if (onCtrlClickUrl && (!lang || lang === "text" || lang === "shell" || lang === "bash" || lang === "powershell" || lang === "console")) {
    const parts = splitTextWithUrls(text);
    const hasUrl = parts.some((p) => p.type === "url");
    if (hasUrl) {
      return (
        <pre className={`code-fence ${className || ""}`.trim()}>
          <code className={className}>
            {parts.map((p, i) =>
              p.type === "text" ? (
                <span key={i}>{p.value}</span>
              ) : (
                <a
                  key={i}
                  href={p.value}
                  className="sandbox-hot-link"
                  title="Ctrl+单击可在浏览器沙盒打开"
                  target="_blank"
                  rel="noreferrer"
                  onClick={(e) => {
                    if (e.ctrlKey || e.metaKey) {
                      e.preventDefault();
                      e.stopPropagation();
                      onCtrlClickUrl(p.value, e.clientX, e.clientY);
                    }
                  }}
                >
                  {p.value}
                </a>
              ),
            )}
          </code>
        </pre>
      );
    }
  }
  return (
    <pre className={`code-fence ${className || ""}`.trim()}>
      <code className={className}>{children}</code>
    </pre>
  );
}

export function MarkdownView({ content, streaming, onCtrlClickUrl }: Props) {
  const body = content || (streaming ? "…" : "");

  const components = useMemo(
    () => ({
      pre({ children }: { children?: ReactNode }) {
        return <>{children}</>;
      },
      code({
        className,
        children,
        ...rest
      }: {
        className?: string;
        children?: ReactNode;
      }) {
        const isBlock =
          Boolean(className?.includes("language-")) || String(children).includes("\n");
        if (!isBlock) {
          return (
            <code className="inline-code" {...rest}>
              {children}
            </code>
          );
        }
        return (
          <CodeBlock
            className={className}
            streaming={streaming}
            onCtrlClickUrl={onCtrlClickUrl}
          >
            {children}
          </CodeBlock>
        );
      },
      a({ href, children }: { href?: string; children?: ReactNode }) {
        return (
          <a
            href={href}
            target="_blank"
            rel="noreferrer"
            className="sandbox-hot-link"
            title="Ctrl+单击可在浏览器沙盒打开"
            onClick={(e) => {
              if (!href || !(e.ctrlKey || e.metaKey)) return;
              if (!/^https?:\/\//i.test(href)) return;
              e.preventDefault();
              e.stopPropagation();
              onCtrlClickUrl?.(href, e.clientX, e.clientY);
            }}
          >
            {children}
          </a>
        );
      },
      table({ children }: { children?: ReactNode }) {
        return (
          <div className="md-table-wrap">
            <table>{children}</table>
          </div>
        );
      },
    }),
    [streaming, onCtrlClickUrl],
  );

  return (
    <div className={`markdown ${streaming ? "is-streaming" : ""}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { plainText: ["mermaid"], detect: true }]]}
        components={components as never}
      >
        {body}
      </ReactMarkdown>
    </div>
  );
}
