(() => {
  const toggle = document.getElementById('theme-toggle');
  const root = document.documentElement;
  if (!toggle) return;

  function updateLabel() {
    const dark = root.dataset.theme === 'dark';
    toggle.textContent = dark ? '☀ Light mode' : '☾ Dark mode';
    toggle.setAttribute('aria-label', dark ? 'Switch to light mode' : 'Switch to dark mode');
    toggle.setAttribute('aria-pressed', String(dark));
  }

  updateLabel();
  toggle.addEventListener('click', () => {
    root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem('quiz-theme', root.dataset.theme); } catch (_) {}
    updateLabel();
  });
})();
