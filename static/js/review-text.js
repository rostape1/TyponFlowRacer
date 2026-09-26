/**
 * The race-review markdown (docs/reviews/*.md, copied to static/reviews/ by tools/race_tracks.py) as
 * DOM, for the crew race page. Only what the reviews use: # headings, paragraphs, - and 1. lists
 * (with indented continuation lines), | tables |, **bold**, *italic*, `code`.
 *
 * Every piece of text goes in with textContent, never innerHTML (P19): the file is fetched, so it
 * is data, not markup. Clock times in the text (13:53:09, ~15:06) become links when linkTime(h, m, s)
 * returns a moment for them; it returns null for anything outside the race (18:17 behind is a gap).
 */
class ReviewText {
    static get TIME_RE() { return /\b([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?\b/g; }

    /** Markdown -> DocumentFragment. linkTime(h, m, s) -> epoch ms or null; onTime(ms) on click. */
    static render(md, { linkTime = () => null, onTime = () => {} } = {}) {
        const doc = document;
        const frag = doc.createDocumentFragment();
        const el = (tag, cls) => { const e = doc.createElement(tag); if (cls) e.className = cls; return e; };
        const lines = String(md).replace(/\r\n?/g, '\n').split('\n');
        const inline = (parent, text) => ReviewText._inline(parent, text, { linkTime, onTime, el });
        let i = 0;
        while (i < lines.length) {
            const line = lines[i];
            if (!line.trim()) { i++; continue; }
            const h = /^(#{1,4})\s+(.*)$/.exec(line);
            if (h) {
                const e = el('h' + Math.min(h[1].length + 1, 5));   // the page has its own h1
                inline(e, h[2]);
                frag.appendChild(e);
                i++;
                continue;
            }
            if (/^\s*\|/.test(line)) {
                const rows = [];
                while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
                frag.appendChild(ReviewText._table(rows, inline, el));
                continue;
            }
            const item = /^(\s*)(-|\*|\d+\.)\s+(.*)$/.exec(line);
            if (item) {
                const ordered = /\d/.test(item[2]);
                const list = el(ordered ? 'ol' : 'ul');
                while (i < lines.length) {
                    const m = /^(\s*)(-|\*|\d+\.)\s+(.*)$/.exec(lines[i]);
                    if (!m || /\d/.test(m[2]) !== ordered) break;
                    let text = m[3];
                    i++;
                    // Continuation: indented lines that don't start a new item.
                    while (i < lines.length && /^\s+\S/.test(lines[i]) && !/^\s*(-|\*|\d+\.)\s/.test(lines[i])) {
                        text += ' ' + lines[i++].trim();
                    }
                    const li = el('li');
                    inline(li, text);
                    list.appendChild(li);
                }
                frag.appendChild(list);
                continue;
            }
            // Paragraph: consecutive plain lines.
            let text = line.trim();
            i++;
            while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*\||\s*(-|\*|\d+\.)\s)/.test(lines[i])) {
                text += ' ' + lines[i++].trim();
            }
            const p = el('p');
            inline(p, text);
            frag.appendChild(p);
        }
        return frag;
    }

    static _table(rows, inline, el) {
        const cells = (r) => r.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
        const table = el('table');
        let head = true;
        for (const r of rows) {
            const c = cells(r);
            if (c.every(x => /^:?-{2,}:?$/.test(x))) { head = false; continue; }   // |---|---|
            const tr = el('tr');
            for (const x of c) {
                const td = el(head ? 'th' : 'td');
                inline(td, x);
                tr.appendChild(td);
            }
            table.appendChild(tr);
            head = false;
        }
        const wrap = el('div', 'review-table');
        wrap.appendChild(table);
        return wrap;
    }

    /** **bold**, *italic*, `code`, then clock times, into parent. */
    static _inline(parent, text, ctx) {
        const re = /\*\*(.+?)\*\*|\*([^*\s][^*]*?)\*|`([^`]+)`/g;
        let last = 0, m;
        while ((m = re.exec(text))) {
            if (m.index > last) ReviewText._times(parent, text.slice(last, m.index), ctx);
            const e = ctx.el(m[1] != null ? 'strong' : m[2] != null ? 'em' : 'code');
            if (m[3] != null) e.textContent = m[3];
            else ReviewText._inline(e, m[1] != null ? m[1] : m[2], ctx);
            parent.appendChild(e);
            last = re.lastIndex;
        }
        if (last < text.length) ReviewText._times(parent, text.slice(last), ctx);
    }

    static _times(parent, text, { linkTime, onTime, el }) {
        const re = ReviewText.TIME_RE;
        re.lastIndex = 0;
        let last = 0, m;
        while ((m = re.exec(text))) {
            const ms = linkTime(+m[1], +m[2], m[3] == null ? 0 : +m[3]);
            if (ms == null) continue;
            if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
            const a = el('a', 'review-time');
            a.href = '#';
            a.textContent = m[0];
            a.dataset.ms = String(ms);
            a.addEventListener('click', (e) => { e.preventDefault(); onTime(ms); });
            parent.appendChild(a);
            last = m.index + m[0].length;
        }
        if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
    }
}

if (typeof window !== 'undefined') window.ReviewText = ReviewText;
