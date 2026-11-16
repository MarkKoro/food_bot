/* Shared Progressor look for Food. It only rearranges and decorates the existing page:
   every native control, id and request stays where the page's own script expects it. */
(() => {
  'use strict';
  const root = document.documentElement, key = 'progressor.ui';
  const icons = {
    cam: 'M5 7h3l2-3h4l2 3h3a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2zM15.5 13a3.5 3.5 0 1 1-7 0 3.5 3.5 0 0 1 7 0z',
    mic: 'M12 3a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3zM5 11a7 7 0 0 0 14 0M12 18v3', pen: 'M4 20h4L19 9l-4-4L4 16v4zM13.5 6.5l4 4', again: 'M4 12a8 8 0 1 0 2.6-5.9M4 4v5h5'};
  const el = (tag, props, ...children) => {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(props || {})) { if (name.startsWith('on')) node.addEventListener(name.slice(2), value); else node.setAttribute(name, value); }
    node.append(...children.filter(child => child != null));
    return node;
  };
  const icon = name => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', icons[name]); svg.append(path); return svg;
  };
  let built = false, hero = null, macros = null, status = null, seg = null;
  // The page keeps its current day in a script-level binding; read it without changing the page.
  const currentDay = () => { try { return typeof lastDayData === 'undefined' ? null : lastDayData; } catch { return null; } };
  const number = value => Math.round(Number(value) || 0).toLocaleString('ru-RU');
  function paint() {
    if (!built) return;
    const day = currentDay(), totals = day?.totals || {}, goal = day?.goal || day?.goals || {};
    const kcal = Number(totals.kcal) || 0, target = Number(goal.kcal) || 0, left = target - kcal;
    hero.querySelector('small').textContent = !target ? 'Цель не задана' : left >= 0 ? 'Осталось' : 'Сверх цели';
    hero.querySelector('strong').textContent = !target ? `${number(kcal)} ккал` : `${number(Math.abs(left))} ккал`;
    hero.querySelector('[data-sub]').textContent = target ? `Съедено ${number(kcal)} из ${number(target)}` : 'Задайте цель в блоке «Профиль и напоминания» ниже';
    hero.querySelector('.u2-bar i').style.width = `${target ? Math.max(0, Math.min(100, kcal / target * 100)) : 0}%`;
    macros.replaceChildren(...[['protein', 'белки'], ['fat', 'жиры'], ['carbs', 'углеводы']].map(([name, label]) =>
      el('div', null, el('b', null, number(totals[name])), el('span', null, goal[name] ? `из ${number(goal[name])} г · ${label}` : `г · ${label}`))));
  }
  function capture(kind) {
    const tool = window.Progressor?.capture;
    if (!tool) { status.textContent = 'Запись ещё загружается. Попробуйте через пару секунд.'; return; }
    status.textContent = ''; tool[kind]();
  }
  function build() {
    if (built) return;
    const today = document.getElementById('screen-today'), app = document.querySelector('.app');
    if (!today || !app) return;
    built = true;
    const screens = [['today', 'Сегодня', 'nav-today'], ['analytics', 'Статистика', 'nav-analytics'], ['subscription', 'Подписка', 'nav-subscription']];
    seg = el('div', {class: 'u2-seg u2-only', role: 'group', 'aria-label': 'Разделы дневника питания'}, ...screens.map(([name, label, id]) =>
      el('button', {type: 'button', 'data-screen': name, 'aria-pressed': 'false', onclick: () => document.getElementById(id)?.click()}, label)));
    app.prepend(el('div', {class: 'u2-head u2-only'}, el('h1', null, 'Еда')), seg);
    const syncSeg = () => { for (const button of seg.children) button.setAttribute('aria-pressed', String(!!document.getElementById('screen-' + button.dataset.screen)?.classList.contains('active'))); };
    for (const [name] of screens) { const screen = document.getElementById('screen-' + name); if (screen) new MutationObserver(syncSeg).observe(screen, {attributes: true, attributeFilter: ['class']}); }
    syncSeg();
    hero = el('div', {class: 'u2-hero u2-only'}, el('small'), el('strong'), el('small', {'data-sub': ''}), el('div', {class: 'u2-bar'}, el('i')),
      el('div', {class: 'u2-actions'}, el('button', {type: 'button', onclick: () => capture('photo')}, icon('cam'), 'Фото'),
        el('button', {type: 'button', 'aria-label': 'Надиктовать, что вы съели', onclick: () => capture('voice')}, icon('mic')),
        el('button', {type: 'button', 'aria-label': 'Написать, что вы съели', onclick: () => capture('text')}, icon('pen')),
        el('button', {type: 'button', 'aria-label': 'Повторить недавнее блюдо', onclick: () => { if (window.Progressor?.repeatMeal) window.Progressor.repeatMeal(); else status.textContent = 'Запись ещё загружается. Попробуйте через пару секунд.'; }}, icon('again'))));
    macros = el('div', {class: 'u2-macros u2-only'});
    // Water and one-tap repeat live in the shared sheets; this row only shows and calls them.
    const water = el('div', {class: 'u2-water u2-only', hidden: ''});
    const paintWater = data => {
      if (!data) return;
      water.removeAttribute('hidden');
      water.replaceChildren(el('span', null, `Вода · ${(data.ml / 1000).toLocaleString('ru-RU', {maximumFractionDigits: 2})} из ${(data.target_ml / 1000).toLocaleString('ru-RU', {maximumFractionDigits: 1})} л`),
        el('button', {type: 'button', 'aria-label': 'Добавить стакан воды, 250 миллилитров', onclick: () => window.Progressor?.water?.add(250)}, '+250'),
        el('button', {type: 'button', 'aria-label': 'Добавить 500 миллилитров воды', onclick: () => window.Progressor?.water?.add(500)}, '+500'));
    };
    document.addEventListener('progressor:water', event => paintWater(event.detail));
    const waitWater = (tries = 0) => { const tool = window.Progressor?.water; if (tool) void tool.get().then(paintWater); else if (tries < 20) setTimeout(() => waitWater(tries + 1), 500); };
    waitWater();
    status = el('p', {class: 'u2-status u2-only', role: 'status'});
    today.prepend(hero, status, macros, water);
    const summary = document.getElementById('today-summary');
    if (summary) new MutationObserver(paint).observe(summary, {childList: true});
    // A meal recorded through the shared sheets belongs to today: reload the page's own day view.
    document.addEventListener('progressor:meal-recorded', () => document.getElementById('btnToday')?.click());
    if (window.Chart) { Chart.defaults.color = '#6B7585'; Chart.defaults.borderColor = '#E6E9EF'; }
    paint();
  }
  function apply(on) {
    try { if (on) localStorage.setItem(key, '2'); else localStorage.removeItem(key); } catch {}
    if (on) { root.dataset.pgUi = '2'; build(); } else delete root.dataset.pgUi;
  }
  if (root.dataset.pgUi === '2') build();
  // The server decides per account; an unavailable Core keeps the last known choice.
  Promise.resolve(window.Progressor?.ready).then(session => { if (session) apply(session.home === true); }).catch(() => {});
})();
