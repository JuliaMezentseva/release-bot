const state = { tab: 'draft' };

function plural(n, one, few, many) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if ([2, 3, 4].includes(mod10) && ![12, 13, 14].includes(mod100)) return few;
  return many;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body && !(opts.body instanceof FormData) ? {'Content-Type': 'application/json'} : undefined,
    ...opts,
  });
  if (res.status === 401) {
    window.location.href = '/admin/login';
    throw new Error('unauthorized');
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

function toast(message, isError = false) {
  const t = document.getElementById('toast');
  t.innerHTML = '';
  if (typeof message === 'string') t.textContent = message;
  else t.appendChild(message);
  t.style.borderLeftColor = isError ? 'var(--danger)' : 'var(--brand)';
  t.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.remove('show'), 4500);
}

function autosize(ta) {
  ta.style.height = 'auto';
  ta.style.height = ta.scrollHeight + 'px';
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// ── Загрузка ────────────────────────────────────────────────────────────

async function refreshBadge() {
  const s = await api('/admin/api/summary');
  const badge = document.getElementById('draftBadge');
  badge.textContent = s.draft_entries;
  badge.classList.toggle('has', s.draft_entries > 0);
}

function switchTab(tab) {
  state.tab = tab;
  document.getElementById('tabDraft').classList.toggle('on', tab === 'draft');
  document.getElementById('tabPublished').classList.toggle('on', tab === 'published');
  load();
}

async function load() {
  const main = document.getElementById('content');
  main.innerHTML = '<div class="empty-state">Загрузка…</div>';
  try {
    const { entries } = await api(`/admin/api/entries?status=${state.tab}`);
    main.innerHTML = '';
    if (!entries.length) {
      main.innerHTML = `<div class="empty-state">${state.tab === 'draft' ? 'Черновиков нет — соберутся сами за сутки, или пришли через бота' : 'Пока ничего не опубликовано'}</div>`;
      return;
    }
    entries.forEach(entry => main.appendChild(state.tab === 'draft' ? buildDraftEntry(entry) : buildPublishedEntry(entry)));
  } catch (e) {
    main.innerHTML = `<div class="empty-state">Не удалось загрузить: ${escapeText(e.message)}</div>`;
  }
  refreshBadge();
}

function escapeText(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function cardCountLabel(n) {
  return `${n} ${plural(n, 'карточка', 'карточки', 'карточек')}`;
}

// ── Черновики ───────────────────────────────────────────────────────────

function buildDraftEntry(entry) {
  const wrap = document.createElement('div');
  wrap.className = 'entry';
  wrap.dataset.entryId = entry.id;

  const head = document.createElement('div');
  head.className = 'entry-head';
  head.innerHTML = `
    <div>
      <div class="entry-date">${escapeText(entry.date_ru || entry.date)}</div>
      <div class="entry-meta">${escapeText(entry.release || '—')} · ${cardCountLabel(entry.cards.length)}</div>
    </div>
    <div class="entry-actions">
      <button class="link-btn" data-act="all">Выбрать все</button>
      <button class="link-btn" data-act="none">Снять все</button>
      <button class="icon-btn" data-act="delete-entry" title="Удалить черновик">✕</button>
      <button class="btn btn-brand btn-sm" data-act="publish">Опубликовать выбранные</button>
    </div>`;
  head.querySelector('[data-act="all"]').onclick = () => setAllChecks(wrap, true);
  head.querySelector('[data-act="none"]').onclick = () => setAllChecks(wrap, false);
  head.querySelector('[data-act="delete-entry"]').onclick = () => deleteEntry(entry.id, wrap);
  head.querySelector('[data-act="publish"]').onclick = () => publishEntry(entry.id, wrap);
  wrap.appendChild(head);

  const body = document.createElement('div');
  body.className = 'entry-body';
  entry.cards.forEach(card => body.appendChild(buildEditableCard(card, entry.id)));
  wrap.appendChild(body);

  return wrap;
}

function setAllChecks(entryNode, value) {
  entryNode.querySelectorAll('.card-edit-head input[type=checkbox]').forEach(cb => cb.checked = value);
}

function buildEditableCard(card, entryId) {
  const box = document.createElement('div');
  box.className = 'card-edit';
  box.dataset.cardId = card.id;

  box.innerHTML = `
    <div class="card-edit-head">
      <input type="checkbox" checked>
      <input type="text" class="title-input" value="">
      <button class="icon-btn" data-act="delete-card" title="Убрать карточку">✕</button>
    </div>
    <div class="field">
      <div class="field-label">Бизнес-ценность <span class="save-status"></span></div>
      <textarea class="field-textarea" rows="1" data-field="business_value"></textarea>
    </div>
    <div class="field">
      <div class="field-label">Описание <span class="save-status"></span></div>
      <textarea class="field-textarea" rows="1" data-field="description"></textarea>
    </div>
    <div class="field">
      <div class="field-label">Теги</div>
      <div class="tags-row"></div>
    </div>
    <div class="field">
      <div class="field-label">Медиа</div>
      <div class="media-row"></div>
    </div>
    <a class="tlink" target="_blank" rel="noopener">Открыть в ЯТ ↗</a>`;

  box.querySelector('.title-input').value = card.title || '';
  box.querySelector('[data-field="business_value"]').value = card.business_value || '';
  box.querySelector('[data-field="description"]').value = card.description || '';
  box.querySelector('.tlink').href = card.url || '#';

  // Автосохранение по каждому текстовому полю — блюр сразу, набор текста с паузой
  const fields = [box.querySelector('.title-input'), ...box.querySelectorAll('.field-textarea')];
  fields.forEach(input => {
    const field = input.classList.contains('title-input') ? 'title' : input.dataset.field;
    const statusEl = input.closest('.field')?.querySelector('.save-status');
    if (input.tagName === 'TEXTAREA') {
      autosize(input);
      input.addEventListener('input', () => autosize(input));
    }
    const save = debounce(() => saveField(card.id, field, input.value, statusEl), 900);
    input.addEventListener('input', save);
    input.addEventListener('blur', () => saveField(card.id, field, input.value, statusEl));
  });

  renderTags(box.querySelector('.tags-row'), card);
  renderMedia(box.querySelector('.media-row'), card);

  box.querySelector('[data-act="delete-card"]').onclick = () => deleteCard(entryId, card.id, box);

  return box;
}

async function saveField(cardId, field, value, statusEl) {
  if (statusEl) { statusEl.textContent = 'Сохраняется…'; statusEl.className = 'save-status show'; }
  try {
    await api(`/admin/api/cards/${cardId}`, { method: 'PATCH', body: JSON.stringify({ [field]: value }) });
    if (statusEl) {
      statusEl.textContent = 'Сохранено';
      statusEl.className = 'save-status show ok';
      setTimeout(() => statusEl.classList.remove('show'), 1500);
    }
  } catch (e) {
    if (statusEl) { statusEl.textContent = 'Ошибка сохранения'; statusEl.className = 'save-status show err'; }
  }
}

function renderTags(container, card) {
  container.innerHTML = '';
  (card.components || []).forEach((tag, i) => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    const label = document.createElement('span');
    label.textContent = tag;
    const rm = document.createElement('button');
    rm.textContent = '✕';
    rm.onclick = async () => {
      const next = card.components.filter((_, idx) => idx !== i);
      await saveTags(card, next, container);
    };
    chip.append(label, rm);
    container.appendChild(chip);
  });
  const input = document.createElement('input');
  input.className = 'tag-input';
  input.placeholder = '+ тег';
  input.addEventListener('keydown', async (e) => {
    if (e.key === 'Enter' && input.value.trim()) {
      e.preventDefault();
      const next = [...(card.components || []), input.value.trim()];
      input.value = '';
      await saveTags(card, next, container);
    }
  });
  container.appendChild(input);
}

async function saveTags(card, next, container) {
  try {
    await api(`/admin/api/cards/${card.id}`, { method: 'PATCH', body: JSON.stringify({ components: next }) });
    card.components = next;
    renderTags(container, card);
  } catch (e) {
    toast('Не удалось сохранить теги: ' + e.message, true);
  }
}

function renderMedia(container, card) {
  container.innerHTML = '';
  card.media = card.media || [];
  card.media.forEach((m, i) => container.appendChild(buildMediaThumb(card, i, m, container)));

  const add = document.createElement('button');
  add.className = 'media-add';
  add.type = 'button';
  add.textContent = '+';
  add.title = 'Добавить фото или видео';
  add.onclick = () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/jpeg,image/png,image/gif,image/webp,video/mp4,video/webm,video/quicktime';
    input.onchange = () => input.files[0] && uploadMedia(card, input.files[0], container);
    input.click();
  };
  container.appendChild(add);
}

function buildMediaThumb(card, index, media, container) {
  const thumb = document.createElement('div');
  thumb.className = 'media-thumb' + (media.type === 'video' ? ' video' : '');
  const src = document.createElement(media.type === 'video' ? 'video' : 'img');
  src.src = '/' + media.local_path;
  thumb.appendChild(src);
  const rm = document.createElement('button');
  rm.className = 'rm';
  rm.textContent = '✕';
  rm.title = 'Удалить';
  rm.onclick = async () => {
    try {
      await api(`/admin/api/cards/${card.id}/media/${index}`, { method: 'DELETE' });
      card.media.splice(index, 1);
      renderMedia(container, card);
    } catch (e) {
      toast('Не удалось удалить файл: ' + e.message, true);
    }
  };
  thumb.appendChild(rm);
  return thumb;
}

async function uploadMedia(card, file, container) {
  const form = new FormData();
  form.append('file', file);
  try {
    const res = await api(`/admin/api/cards/${card.id}/media`, { method: 'POST', body: form });
    card.media.push(res.media);
    renderMedia(container, card);
  } catch (e) {
    toast('Не удалось загрузить файл: ' + e.message, true);
  }
}

async function deleteCard(entryId, cardId, node) {
  if (!confirm('Убрать эту карточку из черновика?')) return;
  try {
    await api(`/admin/api/entries/${entryId}/cards/${cardId}`, { method: 'DELETE' });
    node.remove();
    const entryNode = document.querySelector(`.entry[data-entry-id="${entryId}"]`);
    if (entryNode && !entryNode.querySelector('.card-edit')) { entryNode.remove(); refreshBadge(); }
  } catch (e) {
    toast('Не удалось удалить карточку: ' + e.message, true);
  }
}

async function deleteEntry(entryId, node) {
  if (!confirm('Удалить черновик целиком? Все карточки в нём пропадут.')) return;
  try {
    await api(`/admin/api/entries/${entryId}`, { method: 'DELETE' });
    node.remove();
    refreshBadge();
  } catch (e) {
    toast('Не удалось удалить: ' + e.message, true);
  }
}

async function publishEntry(entryId, node) {
  const checked = [...node.querySelectorAll('.card-edit')].filter(
    c => c.querySelector('input[type=checkbox]').checked
  );
  if (!checked.length) { toast('Выберите хотя бы одну карточку', true); return; }
  if (!confirm(`Опубликовать ${cardCountLabel(checked.length)}? Пост уйдёт в канал.`)) return;

  const cardIds = checked.map(c => c.dataset.cardId);
  try {
    const res = await api(`/admin/api/entries/${entryId}/publish`, {
      method: 'POST', body: JSON.stringify({ card_ids: cardIds }),
    });
    const msg = document.createElement('span');
    msg.textContent = `Опубликовано: ${res.published}. `;
    const link = document.createElement('a');
    link.href = res.page_url; link.target = '_blank'; link.textContent = 'Открыть страницу';
    msg.appendChild(link);
    toast(msg);

    checked.forEach(c => c.remove());
    if (!node.querySelector('.card-edit')) node.remove();
    refreshBadge();
  } catch (e) {
    toast('Не удалось опубликовать: ' + e.message, true);
  }
}

// ── Опубликованное (только просмотр) ────────────────────────────────────

function buildPublishedEntry(entry) {
  const wrap = document.createElement('div');
  wrap.className = 'entry';
  wrap.innerHTML = `
    <div class="entry-head">
      <div>
        <div class="entry-date">${escapeText(entry.date_ru || entry.date)}</div>
        <div class="entry-meta">${cardCountLabel(entry.cards.length)}</div>
      </div>
    </div>
    <div class="entry-body"></div>`;
  const body = wrap.querySelector('.entry-body');
  entry.cards.forEach(card => body.appendChild(buildViewCard(card)));
  return wrap;
}

function buildViewCard(card) {
  const box = document.createElement('div');
  box.className = 'card-view';
  box.innerHTML = `
    <div class="card-view-title"></div>
    <div class="field-label">Бизнес-ценность</div>
    <div class="card-view-text bv"></div>
    <div class="field-label">Описание</div>
    <div class="card-view-text desc"></div>
    <div class="media-row"></div>
    <a class="tlink" target="_blank" rel="noopener" style="display:inline-block;margin-top:8px">Открыть в ЯТ ↗</a>`;
  box.querySelector('.card-view-title').textContent = card.title || '';
  box.querySelector('.bv').textContent = card.business_value || '—';
  box.querySelector('.desc').textContent = card.description || '—';
  box.querySelector('.tlink').href = card.url || '#';
  const mediaRow = box.querySelector('.media-row');
  (card.media || []).forEach((m, i) => {
    const thumb = document.createElement('div');
    thumb.className = 'media-thumb' + (m.type === 'video' ? ' video' : '');
    const src = document.createElement(m.type === 'video' ? 'video' : 'img');
    src.src = '/' + m.local_path;
    thumb.appendChild(src);
    mediaRow.appendChild(thumb);
  });
  return box;
}

// ── Инициализация ────────────────────────────────────────────────────────

document.getElementById('logoutBtn').onclick = async () => {
  await api('/admin/api/logout', { method: 'POST' });
  window.location.href = '/admin/login';
};

(async function init() {
  try {
    await api('/admin/api/me');
  } catch (e) {
    return; // api() уже увёл на /admin/login при 401
  }
  load();
})();
