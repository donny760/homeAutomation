export function mdToHtml(md: string): string {
  const lines = md.split('\n');
  let html = '';
  let inUl = false,
    inOl = false,
    inTable = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();

    if (inUl && !/^[*\-]\s/.test(trimmed) && !/^\s+[*\-]\s/.test(line)) {
      html += '</ul>';
      inUl = false;
    }
    if (inOl && !/^\d+\.\s/.test(trimmed)) {
      html += '</ol>';
      inOl = false;
    }

    if (/^\|(.+)\|$/.test(trimmed)) {
      const cells = trimmed
        .split('|')
        .slice(1, -1)
        .map((c) => c.trim());
      if (cells.every((c) => /^[-:]+$/.test(c))) continue;
      if (!inTable) {
        html += '<table><thead><tr>' + cells.map((c) => '<th>' + c + '</th>').join('') + '</tr></thead><tbody>';
        inTable = true;
        continue;
      }
      html += '<tr>' + cells.map((c) => '<td>' + c + '</td>').join('') + '</tr>';
      continue;
    }
    if (inTable) {
      html += '</tbody></table>';
      inTable = false;
    }

    if (/^###\s(.+)/.test(trimmed)) {
      html += '<h4>' + trimmed.replace(/^###\s/, '') + '</h4>';
      continue;
    }
    if (/^##\s(.+)/.test(trimmed)) {
      html += '<h3>' + trimmed.replace(/^##\s/, '') + '</h3>';
      continue;
    }
    if (/^#\s(.+)/.test(trimmed)) {
      html += '<h2>' + trimmed.replace(/^#\s/, '') + '</h2>';
      continue;
    }

    if (/^---+$/.test(trimmed)) {
      html += '<hr>';
      continue;
    }

    if (/^[*\-]\s+(.*)/.test(trimmed)) {
      if (!inUl) {
        html += '<ul>';
        inUl = true;
      }
      html += '<li>' + trimmed.replace(/^[*\-]\s+/, '') + '</li>';
      continue;
    }
    if (/^\s+[*\-]\s+(.*)/.test(line)) {
      if (!inUl) {
        html += '<ul>';
        inUl = true;
      }
      html += '<li class="ai-sub">' + line.replace(/^\s+[*\-]\s+/, '') + '</li>';
      continue;
    }

    if (/^\d+\.\s+(.*)/.test(trimmed)) {
      if (!inOl) {
        html += '<ol>';
        inOl = true;
      }
      html += '<li>' + trimmed.replace(/^\d+\.\s+/, '') + '</li>';
      continue;
    }

    if (trimmed === '') {
      html += '<br>';
      continue;
    }

    html += '<p>' + trimmed + '</p>';
  }

  if (inUl) html += '</ul>';
  if (inOl) html += '</ol>';
  if (inTable) html += '</tbody></table>';

  html = html
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+?)\*/g, '<em>$1</em>')
    .replace(/`([^`]+?)`/g, '<code>$1</code>');

  return html;
}
