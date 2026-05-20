"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Compact prose renderer for upgrade-report markdown.
 * Uses Tailwind utility classes directly on each element so we don't
 * need the @tailwindcss/typography plugin.
 */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="text-sm leading-relaxed">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: (props) => (
            <h1
              className="mt-6 mb-3 border-b border-border/60 pb-1 text-2xl font-semibold tracking-tight"
              {...props}
            />
          ),
          h2: (props) => (
            <h2
              className="mt-5 mb-2 text-xl font-semibold tracking-tight"
              {...props}
            />
          ),
          h3: (props) => (
            <h3
              className="mt-4 mb-1.5 text-base font-semibold tracking-tight"
              {...props}
            />
          ),
          p: (props) => <p className="my-2" {...props} />,
          ul: (props) => (
            <ul className="my-2 ml-5 list-disc space-y-1" {...props} />
          ),
          ol: (props) => (
            <ol className="my-2 ml-5 list-decimal space-y-1" {...props} />
          ),
          li: (props) => <li className="leading-relaxed" {...props} />,
          a: (props) => (
            <a
              className="text-primary underline-offset-2 hover:underline"
              target="_blank"
              rel="noreferrer"
              {...props}
            />
          ),
          code: ({ className, children, ...props }) => {
            const isBlock = className?.includes("language-");
            if (isBlock) {
              return (
                <code
                  className="block overflow-x-auto rounded-md border border-border/60 bg-muted/40 p-3 font-mono text-xs"
                  {...props}
                >
                  {children}
                </code>
              );
            }
            return (
              <code
                className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]"
                {...props}
              >
                {children}
              </code>
            );
          },
          pre: (props) => <pre className="my-3" {...props} />,
          blockquote: (props) => (
            <blockquote
              className="my-3 border-l-2 border-border pl-3 text-muted-foreground"
              {...props}
            />
          ),
          table: (props) => (
            <div className="my-3 overflow-x-auto rounded-md border border-border/60">
              <table
                className="w-full border-collapse text-xs"
                {...props}
              />
            </div>
          ),
          thead: (props) => <thead className="bg-muted/50" {...props} />,
          th: (props) => (
            <th
              className="border-b border-border/60 px-3 py-1.5 text-left font-medium"
              {...props}
            />
          ),
          td: (props) => (
            <td
              className="border-b border-border/30 px-3 py-1.5 align-top"
              {...props}
            />
          ),
          hr: () => <hr className="my-6 border-border/40" />,
          strong: (props) => (
            <strong className="font-semibold text-foreground" {...props} />
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
