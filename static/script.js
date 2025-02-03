function updateCharCount() {
    const text = document.getElementById('input-text').value;
    const charCount = text.length;
    document.getElementById('char-count').textContent = `文字数: ${charCount}`;
} 