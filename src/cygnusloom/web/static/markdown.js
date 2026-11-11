(() => {
  // A small Markdown renderer for a reply that is still streaming. The server renders the saved
  // reply with markdown-it (commonmark, line breaks on, raw HTML escaped, linkify on); this
  // mirrors that subset closely enough that the preview does not jump when it is replaced. All
  // text is escaped and only known URL schemes are linked, so it is safe to assign as innerHTML.
  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const escapeHtml = (text) => String(text).replace(/[&<>"']/g, (ch) => ESCAPES[ch]);

  // A URL is rejected only when it names a scheme the server would reject too. Entity-encoding
  // a scheme cannot get past this: every URL is escaped again before it reaches the attribute.
  const safeUrl = (raw) => {
    const url = String(raw).trim();
    if (!url || /[\u0000-\u001f\u007f]/.test(url)) return "";
    const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(url);
    if (scheme && !/^(?:https?|mailto|ftp)$/i.test(scheme[1])) return "";
    return url;
  };

  const TRAILING_URL = /[.,;:!?'"”’]+$/;
  const matchBareUrl = (text, start) => {
    if (start > 0 && /[\w/]/.test(text[start - 1])) return null;
    const found = /^(?:https?:\/\/|ftp:\/\/|www\.)[^\s<]+/i.exec(text.slice(start));
    if (!found) return null;
    let shown = found[0].replace(TRAILING_URL, "");
    // A closing parenthesis belongs to the URL only when the URL opened it.
    while (
      shown.endsWith(")") &&
      (shown.match(/\(/g) || []).length < (shown.match(/\)/g) || []).length
    ) {
      shown = shown.slice(0, -1);
    }
    if (!shown) return null;
    const href = /^www\./i.test(shown) ? `http://${shown}` : shown;
    return { href, text: shown, end: start + shown.length };
  };

  const matchAutolink = (text, start) => {
    const close = text.indexOf(">", start + 1);
    if (close === -1) return null;
    const body = text.slice(start + 1, close);
    if (/^[a-z][a-z0-9+.-]{1,31}:[^\s<>]*$/i.test(body)) {
      const href = safeUrl(body);
      return href ? { href, text: body, end: close + 1 } : null;
    }
    if (/^[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+$/.test(body)) {
      return { href: `mailto:${body}`, text: body, end: close + 1 };
    }
    return null;
  };

  const matchLink = (text, start) => {
    let depth = 0;
    let i = start;
    for (; i < text.length; i++) {
      const ch = text[i];
      if (ch === "\\") {
        i++;
        continue;
      }
      if (ch === "[") depth++;
      else if (ch === "]" && --depth === 0) break;
    }
    if (i >= text.length) return null;
    const label = text.slice(start + 1, i);
    let j = i + 1;
    if (text[j] !== "(") return null;
    j++;
    while (text[j] === " " || text[j] === "\t") j++;
    let dest = "";
    if (text[j] === "<") {
      const close = text.indexOf(">", j + 1);
      if (close === -1) return null;
      dest = text.slice(j + 1, close);
      j = close + 1;
    } else {
      const begin = j;
      let nested = 0;
      for (; j < text.length; j++) {
        const ch = text[j];
        if (ch === "\\") {
          j++;
          continue;
        }
        if (/\s/.test(ch)) break;
        if (ch === "(") nested++;
        else if (ch === ")" && nested > 0) nested--;
        else if (ch === ")") break;
      }
      dest = text.slice(begin, j);
    }
    while (text[j] === " " || text[j] === "\t") j++;
    let title = "";
    const quote = text[j];
    if (quote === '"' || quote === "'" || quote === "(") {
      const closing = quote === "(" ? ")" : quote;
      const close = text.indexOf(closing, j + 1);
      if (close === -1) return null;
      title = text.slice(j + 1, close);
      j = close + 1;
      while (text[j] === " " || text[j] === "\t") j++;
    }
    if (text[j] !== ")") return null;
    return { label, dest, title, end: j + 1 };
  };

  const matchEmphasis = (text, start) => {
    const ch = text[start];
    let count = 1;
    while (count < 3 && text[start + count] === ch) count++;
    const next = text[start + count];
    if (next === undefined || /\s/.test(next)) return null;
    // An underscore inside a word is literal, as CommonMark says.
    if (ch === "_" && start > 0 && /\w/.test(text[start - 1])) return null;
    for (let size = count; size >= 1; size--) {
      const marker = ch.repeat(size);
      let i = start + size;
      while (i < text.length) {
        const found = text.indexOf(marker, i);
        if (found === -1) break;
        const before = text[found - 1];
        const after = text[found + size];
        const closes =
          before !== undefined &&
          !/\s/.test(before) &&
          !(ch === "_" && after !== undefined && /\w/.test(after));
        if (closes) {
          const inner = text.slice(start + size, found);
          if (inner.trim()) return { size, inner, end: found + size };
        }
        i = found + 1;
      }
    }
    return null;
  };

  const renderInline = (text) => {
    let out = "";
    let plain = "";
    const flush = () => {
      out += escapeHtml(plain);
      plain = "";
    };
    let i = 0;
    while (i < text.length) {
      const ch = text[i];
      if (ch === "\n") {
        flush();
        out += "<br>\n";
        i++;
        continue;
      }
      if (ch === "\\" && i + 1 < text.length && /[!-/:-@[-`{-~]/.test(text[i + 1])) {
        plain += text[i + 1];
        i += 2;
        continue;
      }
      if (ch === "`") {
        let run = 0;
        while (text[i + run] === "`") run++;
        const end = text.indexOf("`".repeat(run), i + run);
        if (end !== -1) {
          flush();
          let code = text.slice(i + run, end).replace(/\n/g, " ");
          if (code.length > 1 && code.startsWith(" ") && code.endsWith(" ") && code.trim()) {
            code = code.slice(1, -1);
          }
          out += `<code>${escapeHtml(code)}</code>`;
          i = end + run;
          continue;
        }
        plain += ch;
        i++;
        continue;
      }
      if (ch === "[") {
        const link = matchLink(text, i);
        if (link) {
          flush();
          const href = safeUrl(link.dest);
          if (href) {
            const title = link.title ? ` title="${escapeHtml(link.title)}"` : "";
            out += `<a href="${escapeHtml(href)}"${title}>${renderInline(link.label)}</a>`;
          } else {
            out += renderInline(link.label);
          }
          i = link.end;
          continue;
        }
        plain += ch;
        i++;
        continue;
      }
      if (ch === "<") {
        const auto = matchAutolink(text, i);
        if (auto) {
          flush();
          out += `<a href="${escapeHtml(auto.href)}">${escapeHtml(auto.text)}</a>`;
          i = auto.end;
          continue;
        }
        plain += ch;
        i++;
        continue;
      }
      if (ch === "h" || ch === "H" || ch === "w" || ch === "W") {
        const url = matchBareUrl(text, i);
        if (url) {
          flush();
          out += `<a href="${escapeHtml(url.href)}">${escapeHtml(url.text)}</a>`;
          i = url.end;
          continue;
        }
      }
      if (ch === "*" || ch === "_") {
        const emphasis = matchEmphasis(text, i);
        if (emphasis) {
          flush();
          const inner = renderInline(emphasis.inner);
          if (emphasis.size === 3) {
            out += `<em><strong>${inner}</strong></em>`;
          } else {
            const tag = emphasis.size === 2 ? "strong" : "em";
            out += `<${tag}>${inner}</${tag}>`;
          }
          i = emphasis.end;
          continue;
        }
      }
      plain += ch;
      i++;
    }
    flush();
    return out;
  };

  const FENCE = /^ {0,3}(`{3,}|~{3,})[ \t]*(.*)$/;
  const HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*#*[ \t]*$/;
  const THEMATIC = /^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$/;
  const BULLET = /^ {0,3}[-+*][ \t]+(.*)$/;
  const ORDERED = /^ {0,3}(\d{1,9})[.)][ \t]+(.*)$/;
  const QUOTE = /^ {0,3}>[ \t]?(.*)$/;
  const BLANK = /^\s*$/;
  const startsBlock = (line) =>
    FENCE.test(line) || HEADING.test(line) || THEMATIC.test(line) || BULLET.test(line) ||
    ORDERED.test(line) || QUOTE.test(line);

  const renderList = (lines, start) => {
    const ordered = ORDERED.test(lines[start]);
    const first = ordered ? ORDERED.exec(lines[start]) : BULLET.exec(lines[start]);
    if (!first) return null;
    const item = ordered ? (line) => ORDERED.exec(line)?.[2] : (line) => BULLET.exec(line)?.[1];
    const items = [];
    let i = start;
    while (i < lines.length) {
      const content = item(lines[i]);
      if (content === undefined) break;
      const itemLines = [content];
      i++;
      // An indented line continues the item; an unindented one ends the list.
      while (i < lines.length && !BLANK.test(lines[i]) && item(lines[i]) === undefined) {
        if (!/^ {2,}/.test(lines[i])) break;
        itemLines.push(lines[i].trim());
        i++;
      }
      items.push(itemLines.join("\n"));
      // A blank line is part of the list only when another item follows it.
      if (i < lines.length && BLANK.test(lines[i]) && item(lines[i + 1] ?? "") !== undefined) {
        i++;
      }
    }
    const startNumber = ordered ? Number(first[1]) : 1;
    const attrs = ordered && startNumber !== 1 ? ` start="${startNumber}"` : "";
    const tag = ordered ? "ol" : "ul";
    const body = items.map((entry) => `<li>${renderInline(entry)}</li>`).join("\n");
    return { html: `<${tag}${attrs}>\n${body}\n</${tag}>\n`, next: i };
  };

  const renderBlocks = (lines) => {
    let out = "";
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (BLANK.test(line)) {
        i++;
        continue;
      }
      const fence = FENCE.exec(line);
      if (fence) {
        const marker = fence[1];
        const close = new RegExp(`^ {0,3}${marker[0]}{${marker.length},}[ \\t]*$`);
        i++;
        const body = [];
        while (i < lines.length && !close.test(lines[i])) body.push(lines[i++]);
        i++;
        const info = (fence[2] || "").trim();
        const language = info ? ` class="language-${escapeHtml(info)}"` : "";
        const code = body.length ? `${escapeHtml(body.join("\n"))}\n` : "";
        out += `<pre><code${language}>${code}</code></pre>\n`;
        continue;
      }
      const heading = HEADING.exec(line);
      if (heading) {
        const level = heading[1].length;
        out += `<h${level}>${renderInline((heading[2] ?? "").trim())}</h${level}>\n`;
        i++;
        continue;
      }
      if (THEMATIC.test(line)) {
        out += "<hr>\n";
        i++;
        continue;
      }
      if (QUOTE.test(line)) {
        const inner = [];
        while (i < lines.length) {
          const quoted = QUOTE.exec(lines[i]);
          if (quoted) {
            inner.push(quoted[1]);
            i++;
          } else if (!BLANK.test(lines[i]) && !startsBlock(lines[i])) {
            inner.push(lines[i]);
            i++;
          } else {
            break;
          }
        }
        out += `<blockquote>\n${renderBlocks(inner)}</blockquote>\n`;
        continue;
      }
      const list = renderList(lines, i);
      if (list) {
        out += list.html;
        i = list.next;
        continue;
      }
      const paragraph = [];
      while (i < lines.length && !BLANK.test(lines[i])) {
        if (paragraph.length && startsBlock(lines[i])) break;
        paragraph.push(lines[i].trim());
        i++;
      }
      out += `<p>${renderInline(paragraph.join("\n"))}</p>\n`;
    }
    return out;
  };

  const render = (text) => renderBlocks(String(text ?? "").replace(/\r\n?/g, "\n").split("\n"));

  window.renderMarkdown = render;
})();
