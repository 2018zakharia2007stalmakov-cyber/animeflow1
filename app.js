// ── Mobile menu ────────────────────────────────────────────────────────────
function openMobile() {
  document.getElementById('mobile-overlay').classList.remove('hidden');
  document.getElementById('mobile-sidebar').classList.remove('translate-x-full');
}
function closeMobile() {
  document.getElementById('mobile-overlay').classList.add('hidden');
  document.getElementById('mobile-sidebar').classList.add('translate-x-full');
}
window.openMobile = openMobile;
window.closeMobile = closeMobile;

// ── Search (dropdown + SPA main-content update) ────────────────────────────
(function () {
  var input   = document.getElementById('search-input');
  var results = document.getElementById('search-results');
  if (!input || !results) return;

  var debounceTimer = null;
  var lastQuery = '';

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function hideDropdown() {
    results.classList.add('hidden');
    results.innerHTML = '';
  }
  function showDropdown(html) {
    results.innerHTML = html;
    results.classList.remove('hidden');
  }

  // ── Dropdown (fires on every keystroke) ──────────────────────────────────
  function fetchDropdown(q) {
    fetch('/api/search?q=' + encodeURIComponent(q) + '&limit=8')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (q !== lastQuery) return;
        if (!data.results || data.results.length === 0) {
          showDropdown('<div class="px-4 py-3 text-sm text-gray-500">Ничего не найдено</div>');
          return;
        }
        showDropdown(
          data.results.map(function (a) {
            return (
              '<a href="/anime/' + a.id + '" class="flex items-center gap-3 px-3 py-2 hover:bg-white/5 transition">' +
              '<div class="w-10 h-14 rounded-md overflow-hidden bg-surface2 shrink-0">' +
              (a.poster_url ? '<img src="' + escapeHtml(a.poster_url) + '" class="w-full h-full object-cover" loading="lazy" onerror="this.onerror=null;this.src=\'/static/images/default-cover.jpg\'" />' : '') +
              '</div>' +
              '<div class="min-w-0">' +
              '<div class="text-sm font-semibold truncate">' + escapeHtml(a.title) + '</div>' +
              '<div class="text-xs text-gray-500">' + escapeHtml(String(a.year || '')) + '</div>' +
              '</div></a>'
            );
          }).join('')
        );
      })
      .catch(hideDropdown);
  }

  // ── Build an anime-card for the main grid (SPA) ──────────────────────────
  function buildCard(a) {
    var poster = escapeHtml(a.poster_url || '/static/images/default-cover.jpg');
    var ratingHtml = a.rating
      ? '<div class="absolute top-2 right-2 bg-black/60 backdrop-blur px-2 py-0.5 rounded-lg text-xs font-bold text-yellow-400 flex items-center gap-1">' +
        '<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M12 .587l3.668 7.568L24 9.75l-6 5.844 1.417 8.262L12 19.771 4.583 23.856 6 15.594 0 9.75l8.332-1.595z"/></svg>' +
        parseFloat(a.rating).toFixed(1) + '</div>'
      : '';
    return (
      '<a href="/anime/' + a.id + '" class="anime-card group block relative rounded-2xl overflow-hidden bg-surface border border-white/5 transition-all duration-300 hover:scale-[1.04] hover:shadow-glow hover:border-accent/40">' +
      '<div class="aspect-[2/3] relative overflow-hidden bg-surface2">' +
      '<img src="' + poster + '" alt="' + escapeHtml(a.title) + '" loading="lazy"' +
      ' class="w-full h-full object-cover transition duration-500 group-hover:scale-110"' +
      ' onerror="this.onerror=null;this.src=\'/static/images/default-cover.jpg\'" />' +
      '<div class="absolute inset-0 bg-gradient-to-t from-black/90 via-black/30 to-transparent opacity-0 group-hover:opacity-100 transition duration-300 grid place-items-center">' +
      '<div class="w-14 h-14 rounded-full bg-accent grid place-items-center shadow-glow scale-90 group-hover:scale-100 transition">' +
      '<svg class="w-6 h-6 text-white ml-1" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>' +
      '</div></div>' + ratingHtml +
      '</div>' +
      '<div class="p-3">' +
      '<h3 class="font-semibold text-sm leading-tight line-clamp-1 group-hover:text-accent transition">' + escapeHtml(a.title) + '</h3>' +
      '<div class="flex items-center justify-between mt-1 text-xs text-gray-400">' +
      '<span>' + escapeHtml(String(a.year || '—')) + '</span>' +
      '<span class="capitalize">' + escapeHtml(a.status || '') + '</span>' +
      '</div></div></a>'
    );
  }

  // ── Update main catalog in-place (SPA on / and /search) ──────────────────
  function updateMainContent(q) {
    var section = document.getElementById('catalog-section');
    var heading = document.getElementById('catalog-heading');
    var sub     = document.getElementById('catalog-subheading');
    var grid    = document.getElementById('anime-grid');
    var pager   = document.getElementById('pagination');
    var counter = document.getElementById('catalog-counter');
    if (!section || !grid) return;

    section.style.opacity = '0.5';
    section.style.pointerEvents = 'none';

    fetch('/api/search?q=' + encodeURIComponent(q) + '&limit=96')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var items = data.results || [];
        if (heading) heading.textContent = q ? 'Результаты поиска' : 'Каталог аниме';
        if (sub) {
          sub.textContent = q
            ? (items.length > 0 ? 'Найдено: ' + items.length : 'По вашему запросу ничего не найдено.')
            : 'Лучшее из мира аниме — смотри без рекламы, в любом качестве.';
        }
        if (counter) counter.textContent = items.length + ' тайтлов';

        if (items.length === 0) {
          grid.innerHTML =
            '<div class="col-span-full rounded-2xl border border-white/5 bg-surface p-12 text-center">' +
            '<div class="text-5xl mb-3">🔍</div>' +
            '<p class="text-gray-300 font-semibold">Ничего не найдено</p>' +
            '<p class="text-gray-500 text-sm mt-1">Попробуйте другой запрос.</p>' +
            '</div>';
        } else {
          grid.innerHTML = items.map(buildCard).join('');
        }
        if (pager) pager.style.display = (q && items.length > 0) ? 'none' : '';
        history.pushState({ q: q }, '', q ? '/search?q=' + encodeURIComponent(q) : '/');
      })
      .catch(function () {
        if (grid) grid.innerHTML =
          '<div class="col-span-full text-center text-gray-500 py-8">Ошибка поиска. Попробуйте ещё раз.</div>';
      })
      .finally(function () {
        section.style.opacity = '';
        section.style.pointerEvents = '';
      });
  }

  // ── Input: dropdown on keystroke ─────────────────────────────────────────
  input.addEventListener('input', function (e) {
    var q = e.target.value.trim();
    lastQuery = q;
    clearTimeout(debounceTimer);
    if (q.length < 1) { hideDropdown(); return; }
    debounceTimer = setTimeout(function () { fetchDropdown(q); }, 200);
  });

  // ── Enter/submit: SPA on home + search pages ──────────────────────────────
  function trySpaTrigger(e) {
    var q    = input.value.trim();
    var path = window.location.pathname;
    if (path !== '/' && path !== '/search') return;
    if (e) e.preventDefault();
    hideDropdown();
    updateMainContent(q);
  }

  var form = input.closest('form');
  if (form) form.addEventListener('submit', trySpaTrigger);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') trySpaTrigger(e);
  });

  // ── Close dropdown on outside click ──────────────────────────────────────
  document.addEventListener('click', function (e) {
    var wrap = document.getElementById('search-wrap');
    if (wrap && !wrap.contains(e.target)) hideDropdown();
  });
  input.addEventListener('focus', function () {
    if (input.value.trim().length > 0 && results.innerHTML) {
      results.classList.remove('hidden');
    }
  });

  // ── Back / forward ────────────────────────────────────────────────────────
  window.addEventListener('popstate', function (e) {
    var state = (e && e.state) || {};
    var path  = window.location.pathname;
    if (path === '/' || path === '/search') {
      var q = state.q || new URLSearchParams(window.location.search).get('q') || '';
      input.value = q;
      if (q) { updateMainContent(q); } else { window.location.reload(); }
    }
  });

  // ── Pre-fill from URL on load ─────────────────────────────────────────────
  (function () {
    var params = new URLSearchParams(window.location.search);
    var qParam = (params.get('q') || '').trim();
    if (qParam && document.getElementById('anime-grid')) {
      input.value = qParam;
    }
  })();
})();

// ── "Scroll to top" button ────────────────────────────────────────────────
(function () {
  var btn = document.getElementById('scroll-top');
  if (!btn) return;
  function onScroll() {
    if (window.scrollY > 400) {
      btn.classList.remove('opacity-0', 'pointer-events-none', 'translate-y-3');
    } else {
      btn.classList.add('opacity-0', 'pointer-events-none', 'translate-y-3');
    }
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  btn.addEventListener('click', function () { window.scrollTo({ top: 0, behavior: 'smooth' }); });
  onScroll();
})();
