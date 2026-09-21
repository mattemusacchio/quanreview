class TextSpan {
    constructor(start, end, text, type = null, metadata = {}) {
        this.start = start;
        this.end = end;
        this.text = text;
        this.type = type;
        this.metadata = metadata;
    }

    length() {
        return this.end - this.start;
    }

    overlaps(other) {
        return this.start < other.end && other.start < this.end;
    }

    contains(position) {
        return position >= this.start && position < this.end;
    }
}

function resolveSpanRange(text, preferredStart, preferredEnd, expectedText, searchRadius = 80) {
    const hasPreferredRange =
        Number.isInteger(preferredStart) &&
        Number.isInteger(preferredEnd) &&
        preferredStart >= 0 &&
        preferredEnd <= text.length &&
        preferredStart < preferredEnd;

    if (!expectedText || typeof expectedText !== 'string') {
        return hasPreferredRange ? { start: preferredStart, end: preferredEnd } : null;
    }

    if (hasPreferredRange) {
        const excerpt = text.substring(preferredStart, preferredEnd);
        if (excerpt === expectedText || excerpt.toLowerCase() === expectedText.toLowerCase()) {
            return { start: preferredStart, end: preferredEnd };
        }
    }

    const searchStart = Math.max(0, (preferredStart ?? 0) - searchRadius);
    const searchEnd = Math.min(text.length, (preferredEnd ?? 0) + searchRadius);
    const windowText = text.substring(searchStart, searchEnd);

    const exactIndex = windowText.indexOf(expectedText);
    if (exactIndex !== -1) {
        const start = searchStart + exactIndex;
        return { start, end: start + expectedText.length };
    }

    const lowerWindow = windowText.toLowerCase();
    const lowerExpected = expectedText.toLowerCase();
    const caseInsensitiveIndex = lowerWindow.indexOf(lowerExpected);
    if (caseInsensitiveIndex !== -1) {
        const start = searchStart + caseInsensitiveIndex;
        return { start, end: start + expectedText.length };
    }

    return hasPreferredRange ? { start: preferredStart, end: preferredEnd } : null;
}

class TextHighlighter {
    constructor(text) {
        this.text = text;
        this.spans = [];
    }

    addSpan(span) {
        // Validate span boundaries when adding
        if (span.start < 0 || span.end > this.text.length || span.start >= span.end) {
            console.error('Invalid span boundaries:', span);
            return this;
        }
        
        this.spans.push(span);
        return this;
    }

    highlightText() {
        if (this.spans.length === 0) {
            return this._escapeHtml(this.text);
        }

        // Build sorted set of boundary positions so each segment is covered by a
        // stable set of spans. Overlapping spans become distinct segments that
        // can be rendered with a blended color.
        const points = new Set([0, this.text.length]);
        for (const span of this.spans) {
            points.add(span.start);
            points.add(span.end);
        }
        const boundaries = [...points].sort((a, b) => a - b);

        const parts = [];
        for (let i = 0; i < boundaries.length - 1; i++) {
            const segStart = boundaries[i];
            const segEnd = boundaries[i + 1];
            if (segStart >= segEnd) continue;

            const segText = this._escapeHtml(this.text.substring(segStart, segEnd));
            const covering = this.spans.filter(s => s.start <= segStart && s.end >= segEnd);

            if (covering.length === 0) {
                parts.push(segText);
                continue;
            }

            if (covering.length === 1) {
                const span = covering[0];
                const spanClass = span.metadata.isCurrentTag ? 'current-tag' : span.type;
                const title = `Position: ${span.metadata.originalStart}-${span.metadata.originalEnd}`;
                const style = this._buildInlineStyle(span.metadata);
                parts.push(
                    `<span class="highlight ${spanClass}" title="${title}"${style}>` +
                    segText +
                    '</span>'
                );
                continue;
            }

            // Overlap: blend background colors and merge titles/classes.
            const blended = this._blendBackgroundColors(
                covering.map(s => s.metadata && s.metadata.backgroundColor).filter(Boolean)
            );
            const classList = new Set();
            for (const s of covering) {
                classList.add(s.metadata.isCurrentTag ? 'current-tag' : s.type);
            }
            const titleParts = covering
                .map(s => `${s.metadata.originalStart}-${s.metadata.originalEnd}`)
                .join(', ');
            const styleAttr = blended ? ` style="background-color: ${blended};"` : '';
            parts.push(
                `<span class="highlight ${[...classList].join(' ')}" title="Overlapping: ${titleParts}"${styleAttr}>` +
                segText +
                '</span>'
            );
        }

        return parts.join('');
    }

    _blendBackgroundColors(colors) {
        const rgbs = colors.map(c => TextHighlighter._parseColorToRgb(c)).filter(Boolean);
        if (rgbs.length === 0) return '';
        const r = Math.round(rgbs.reduce((a, c) => a + c[0], 0) / rgbs.length);
        const g = Math.round(rgbs.reduce((a, c) => a + c[1], 0) / rgbs.length);
        const b = Math.round(rgbs.reduce((a, c) => a + c[2], 0) / rgbs.length);
        return `rgba(${r}, ${g}, ${b}, 0.65)`;
    }

    static _parseColorToRgb(color) {
        if (!color) return null;
        const str = String(color).trim();

        const hsl = /^hsla?\(\s*([\d.]+)\s*,\s*([\d.]+)%\s*,\s*([\d.]+)%/i.exec(str);
        if (hsl) {
            return TextHighlighter._hslToRgb(
                parseFloat(hsl[1]),
                parseFloat(hsl[2]),
                parseFloat(hsl[3])
            );
        }

        const hex6 = /^#([0-9a-f]{6})$/i.exec(str);
        if (hex6) {
            const n = parseInt(hex6[1], 16);
            return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
        }

        const hex3 = /^#([0-9a-f]{3})$/i.exec(str);
        if (hex3) {
            const s = hex3[1];
            return [
                parseInt(s[0] + s[0], 16),
                parseInt(s[1] + s[1], 16),
                parseInt(s[2] + s[2], 16),
            ];
        }

        const rgb = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)/i.exec(str);
        if (rgb) {
            return [
                Math.round(parseFloat(rgb[1])),
                Math.round(parseFloat(rgb[2])),
                Math.round(parseFloat(rgb[3])),
            ];
        }

        return null;
    }

    static _hslToRgb(h, s, l) {
        s = s / 100;
        l = l / 100;
        const k = n => (n + h / 30) % 12;
        const a = s * Math.min(l, 1 - l);
        const f = n => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
        return [
            Math.round(255 * f(0)),
            Math.round(255 * f(8)),
            Math.round(255 * f(4)),
        ];
    }

    _escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    _buildInlineStyle(metadata) {
        if (!metadata || !metadata.backgroundColor) {
            return '';
        }

        const safeColor = String(metadata.backgroundColor).replace(/"/g, '&quot;');
        return ` style="background-color: ${safeColor};"`;
    }
}

// Export for testing
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { TextSpan, TextHighlighter, resolveSpanRange };
} 
