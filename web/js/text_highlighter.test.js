const { TextSpan, TextHighlighter, resolveSpanRange } = require('./text_highlighter');

// Mock document.createElement for Node.js environment
if (typeof document === 'undefined') {
    global.document = {
        createElement: () => ({
            set textContent(text) {
                this.content = text;
            },
            get innerHTML() {
                return this.content.replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#039;');
            }
        })
    };
}

describe('TextSpan', () => {
    test('should correctly calculate length', () => {
        const span = new TextSpan(5, 10, 'hello');
        expect(span.length()).toBe(5);
    });

    test('should detect overlapping spans', () => {
        const span1 = new TextSpan(5, 10, 'hello');
        const span2 = new TextSpan(8, 15, 'world');
        const span3 = new TextSpan(11, 15, 'test');
        
        expect(span1.overlaps(span2)).toBe(true);
        expect(span1.overlaps(span3)).toBe(false);
    });

    test('should detect if position is contained', () => {
        const span = new TextSpan(5, 10, 'hello');
        expect(span.contains(7)).toBe(true);
        expect(span.contains(4)).toBe(false);
        expect(span.contains(10)).toBe(false);
    });
});

describe('TextHighlighter', () => {
    test('should highlight single span correctly', () => {
        const text = 'Hello world';
        const highlighter = new TextHighlighter(text);
        highlighter.addSpan(new TextSpan(0, 5, 'Hello', 'test-tag'));
        
        const expected = '<span class="highlight test-tag" title="Position: undefined-undefined">Hello</span> world';
        expect(highlighter.highlightText()).toBe(expected);
    });

    test('should handle multiple non-overlapping spans', () => {
        const text = 'Hello world test';
        const highlighter = new TextHighlighter(text);
        
        highlighter
            .addSpan(new TextSpan(0, 5, 'Hello', 'tag1'))
            .addSpan(new TextSpan(6, 11, 'world', 'tag2'));
        
        const expected = '<span class="highlight tag1" title="Position: undefined-undefined">Hello</span> ' +
                        '<span class="highlight tag2" title="Position: undefined-undefined">world</span> test';
        expect(highlighter.highlightText()).toBe(expected);
    });

    test('should handle current tag highlighting', () => {
        const text = 'Hello world';
        const highlighter = new TextHighlighter(text);
        
        highlighter.addSpan(new TextSpan(0, 5, 'Hello', 'tag1', {
            isCurrentTag: true,
            originalStart: 0,
            originalEnd: 5
        }));
        
        const expected = '<span class="highlight current-tag" title="Position: 0-5">Hello</span> world';
        expect(highlighter.highlightText()).toBe(expected);
    });

    test('should include inline background colors for dynamic field highlights', () => {
        const text = 'northeast states are affected';
        const highlighter = new TextHighlighter(text);

        highlighter.addSpan(new TextSpan(0, 16, 'northeast states', 'highlight', {
            backgroundColor: 'hsl(210, 70%, 85%)',
            originalStart: 0,
            originalEnd: 16
        }));

        const expected = '<span class="highlight highlight" title="Position: 0-16" style="background-color: hsl(210, 70%, 85%);">northeast states</span> are affected';
        expect(highlighter.highlightText()).toBe(expected);
    });

    test('should escape HTML in text', () => {
        const text = 'Hello <world>';
        const highlighter = new TextHighlighter(text);
        
        highlighter.addSpan(new TextSpan(0, 5, 'Hello', 'tag1'));
        
        const expected = '<span class="highlight tag1" title="Position: undefined-undefined">Hello</span> &lt;world&gt;';
        expect(highlighter.highlightText()).toBe(expected);
    });

    test('should handle spans in reverse order correctly', () => {
        const text = 'Hello world test';
        const highlighter = new TextHighlighter(text);
        
        // Add spans in reverse order with correct positions
        highlighter
            .addSpan(new TextSpan(6, 11, 'world', 'tag2'))  // "world"
            .addSpan(new TextSpan(0, 5, 'Hello', 'tag1'));  // "Hello"
        
        const expected = '<span class="highlight tag1" title="Position: undefined-undefined">Hello</span> ' +
                        '<span class="highlight tag2" title="Position: undefined-undefined">world</span> test';
        expect(highlighter.highlightText()).toBe(expected);
    });
});

describe('resolveSpanRange', () => {
    test('should keep the preferred range when the text already matches', () => {
        const text = 'About 1.8 million hectares';
        expect(resolveSpanRange(text, 6, 17, '1.8 million')).toEqual({ start: 6, end: 17 });
    });

    test('should recover the nearest matching text when offsets are misaligned', () => {
        const text = 'About 1.8 million hectares (out of 15.4 million) are arable, out of which 300 000 hectares';
        expect(resolveSpanRange(text, 60, 89, '300 000')).toEqual({ start: 74, end: 81 });
    });

    test('should match case-insensitively when needed', () => {
        const text = 'About 1.8 million hectares';
        expect(resolveSpanRange(text, 0, 5, 'about')).toEqual({ start: 0, end: 5 });
    });
});

describe('TextHighlighter with real data', () => {
    test('should correctly highlight named entities from example data', () => {
        const text = "Apple Inc. is planning to open a new office in Seattle, Washington next month. CEO Tim Cook made the announcement yesterday.";
        const highlighter = new TextHighlighter(text);
        
        // Add annotated tags with correct positions from Python script
        const realTags = [
            { text: "Apple Inc.", start: 0, end: 10 },
            { text: "Seattle", start: 47, end: 54 },
            { text: "Washington", start: 56, end: 66 },
            { text: "Tim Cook", start: 83, end: 91 }
        ];
        
        realTags.forEach(tag => {
            highlighter.addSpan(new TextSpan(
                tag.start,
                tag.end,
                tag.text,
                'real-tag',
                { originalStart: tag.start, originalEnd: tag.end }
            ));
        });
        
        const expected = '<span class="highlight real-tag" title="Position: 0-10">Apple Inc.</span>' +
                        ' is planning to open a new office in ' +
                        '<span class="highlight real-tag" title="Position: 47-54">Seattle</span>' +
                        ', ' +
                        '<span class="highlight real-tag" title="Position: 56-66">Washington</span>' +
                        ' next month. CEO ' +
                        '<span class="highlight real-tag" title="Position: 83-91">Tim Cook</span>' +
                        ' made the announcement yesterday.';
        
        expect(highlighter.highlightText()).toBe(expected);
    });

    test('should handle false positives case from example data', () => {
        const text = "Apple Inc. is planning to open a new office in Seattle, Washington next month. CEO Tim Cook made the announcement yesterday.";
        const highlighter = new TextHighlighter(text);
        
        // Add model predicted tags with correct positions from Python script
        const modelTags = [
            { text: "Apple Inc.", start: 0, end: 10 },
            { text: "Seattle", start: 47, end: 54 },
            { text: "Washington", start: 56, end: 66 },
            { text: "Tim", start: 83, end: 86 }  // This is a false positive
        ];
        
        // Add the current tag being validated
        const currentTag = { text: "Tim", start: 83, end: 86 };
        
        modelTags.forEach(tag => {
            const isCurrentTag = tag.start === currentTag.start && tag.end === currentTag.end;
            highlighter.addSpan(new TextSpan(
                tag.start,
                tag.end,
                tag.text,
                'model-tag',
                { isCurrentTag, originalStart: tag.start, originalEnd: tag.end }
            ));
        });
        
        const expected = '<span class="highlight model-tag" title="Position: 0-10">Apple Inc.</span>' +
                        ' is planning to open a new office in ' +
                        '<span class="highlight model-tag" title="Position: 47-54">Seattle</span>' +
                        ', ' +
                        '<span class="highlight model-tag" title="Position: 56-66">Washington</span>' +
                        ' next month. CEO ' +
                        '<span class="highlight current-tag" title="Position: 83-86">Tim</span>' +
                        ' Cook made the announcement yesterday.';
        
        expect(highlighter.highlightText()).toBe(expected);
    });
}); 
