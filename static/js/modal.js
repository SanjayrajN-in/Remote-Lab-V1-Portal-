/* Shared modal open/close for admin screens. */
(function () {
  function close(modal) { modal.classList.remove('open'); }

  document.querySelectorAll('[data-open]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var modal = document.getElementById(btn.dataset.open);
      if (!modal) return;
      modal.classList.add('open');
      var first = modal.querySelector('input, select, textarea, button');
      if (first) first.focus();
    });
  });

  document.querySelectorAll('.modal').forEach(function (modal) {
    modal.querySelectorAll('[data-close]').forEach(function (el) {
      el.addEventListener('click', function () { close(modal); });
    });
    modal.addEventListener('click', function (e) { if (e.target === modal) close(modal); });
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('.modal.open').forEach(close);
  });
})();
