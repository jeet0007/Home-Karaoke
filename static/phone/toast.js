// Tiny bottom-of-screen confirmation toast (e.g. "Added to the queue").

const toastEl = document.getElementById('toast');
let hideTimer = null;

export function showToast(message, durationMs = 2000) {
  toastEl.textContent = message;
  toastEl.classList.add('visible');
  if (hideTimer) clearTimeout(hideTimer);
  hideTimer = setTimeout(() => toastEl.classList.remove('visible'), durationMs);
}
