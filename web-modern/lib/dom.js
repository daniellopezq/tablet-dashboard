// Tiny element builder: h('div', {class: 'x', onclick: fn}, 'text', child, [more])
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs ?? {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else el.append(c instanceof Node ? c : String(c));
  }
}

export const icon = (name, cls = 'icon') => h('img', { class: cls, src: `/icons/${name}.svg`, alt: '' });

export function replace(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

/** A card with a header row (title + optional extra) and a body. */
export function card({ title, iconName, extra, href, cls = '' }, ...body) {
  const head = h('div', { class: 'card-head' },
    iconName && icon(iconName, 'card-icon'),
    href ? h('a', { class: 'card-title', href }, title) : h('h2', { class: 'card-title' }, title),
    extra && h('span', { class: 'card-extra' }, extra));
  return h('section', { class: `card ${cls}` }, head, h('div', { class: 'card-body' }, ...body));
}

export function message(title, body, cls = '') {
  return h('div', { class: `msg ${cls}` }, h('strong', null, title), body && h('p', null, body));
}

/** Short message at the bottom of the screen. */
let toastTimer = null;
export function toast(text, ms = 4000) {
  let el = document.getElementById('toast');
  if (!el) {
    el = h('div', { id: 'toast', class: 'toast', role: 'status', 'aria-live': 'polite' });
    document.body.append(el);
  }
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), ms);
}

/** Modal yes/cancel question. Resolves true for yes. */
export function confirmDialog({ title, body, yes = 'Yes', danger = false }) {
  return new Promise((resolve) => {
    const prev = document.activeElement;
    const done = (answer) => {
      document.removeEventListener('keydown', onKey);
      wrap.remove();
      prev?.focus?.();
      resolve(answer);
    };
    const onKey = (e) => { if (e.key === 'Escape') done(false); };
    const yesBtn = h('button', { type: 'button', class: `btn ${danger ? 'btn-danger' : ''}`, onclick: () => done(true) }, yes);
    const wrap = h('div', { class: 'modal', onclick: (e) => { if (e.target === wrap) done(false); } },
      h('div', { class: 'modal-panel', role: 'alertdialog', 'aria-modal': 'true', 'aria-label': title },
        h('h2', { class: 'modal-title' }, title),
        body && h('p', { class: 'modal-body' }, body),
        h('div', { class: 'modal-actions' },
          h('button', { type: 'button', class: 'btn btn-quiet', onclick: () => done(false) }, 'Cancel'), yesBtn)));
    document.addEventListener('keydown', onKey);
    document.body.append(wrap);
    yesBtn.focus();
  });
}
