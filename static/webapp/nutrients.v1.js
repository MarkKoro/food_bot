/* Food-owned nutrient editor. Auth, schema and Core contract: docs/PROGRESSOR_INTEGRATION.md. */
(() => {
  function node(tag, text, parent) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (parent) parent.append(n); return n; }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    const proof = window.Telegram?.WebApp?.initData || window.Progressor?.launchData || '';
    if (proof) headers.set('X-Tg-Init-Data', proof);
    const response = await fetch('/api/nutrients/' + path, {...options, headers});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось сохранить запись');
    return data;
  }
  function mount() {
    const root = document.querySelector('.app'); if (!root) return;
    const details = node('details'); details.id = 'food-nutrients-editor'; details.className = 'card';
    node('summary', 'Микронутриенты и состав пищи', details);
    node('p', 'Для новых фото и описаний Food автоматически оценивает состав пищи и считает количество за съеденную порцию. Оценки приблизительные; неизвестные значения остаются пустыми. Здесь можно посмотреть результат и при необходимости уточнить его по этикетке.', details);
    const message = node('p', 'Откройте, чтобы загрузить ваши приёмы пищи.', details); message.setAttribute('role', 'status');
    const form = node('form', undefined, details); form.hidden = true;
    const make = (tag, label) => { const wrapper = node('label', undefined, form); wrapper.style.cssText = 'display:grid;gap:6px;margin:12px 0'; node('span', label, wrapper); const el = node(tag, undefined, wrapper); el.style.cssText = 'min-height:44px;max-width:100%;padding:8px;border-radius:8px;background:#17243b;color:#fff;border:1px solid #64748b'; return el; };
    const mealSelect = make('select', 'Приём пищи'); mealSelect.setAttribute('aria-label', 'Приём пищи для нутриентов');
    const nutrientSelect = make('select', 'Нутриент'); nutrientSelect.setAttribute('aria-label', 'Нутриент');
    const value = make('input', 'Количество за всю порцию'); value.type = 'text'; value.inputMode = 'decimal'; value.setAttribute('aria-label', 'Количество нутриента');
    const unit = node('p', undefined, form);
    const origin = make('select', 'Источник'); for (const [v, t] of [['user_entry','Мои данные'],['food_label','Этикетка продукта']]) { const o = node('option', t, origin); o.value = v; }
    const reference = make('input', 'Название продукта или источник (необязательно)'); reference.maxLength = 512;
    const existing = node('p', undefined, form);
    const composition = node('details', undefined, form);
    node('summary', 'Весь рассчитанный состав порции', composition);
    const compositionRows = node('div', undefined, composition);
    const save = node('button', 'Сохранить', form); save.type = 'submit'; save.style.cssText = 'min-height:44px;padding:8px 18px;background:#2563eb;color:white;border:0;border-radius:8px';
    let catalog = [], meals = [];
    const selected = () => meals.find(m => String(m.id) === mealSelect.value);
    function fill() {
      const meal = selected(); const code = nutrientSelect.value; const observed = meal?.observations.find(o => o.nutrient_code === code);
      const nutrient = catalog.find(n => n.code === code); unit.textContent = `Единица: ${nutrient?.unit === 'ug' ? 'мкг' : nutrient?.unit === 'mg' ? 'мг' : 'г'}. Пусто — нет данных.`;
      value.value = observed && !observed.stale && observed.value !== null ? String(observed.value) : '';
      origin.value = observed?.source === 'food_label' ? 'food_label' : 'user_entry'; reference.value = observed?.source === 'ai_estimate' ? '' : observed?.reference || '';
      let coverage = {}; try { coverage = JSON.parse(observed?.coverage_json || '{}'); } catch (_) {}
      existing.textContent = observed?.stale ? 'Порция изменилась. Ваше прежнее уточнение исключено из расчётов; при необходимости подтвердите количество для новой порции.' : observed?.source === 'ai_estimate' ? `Приблизительная оценка ИИ. Состав известен для ${coverage.observed_components || 0} из ${coverage.total_components || 0} продуктов в этой порции. Можно уточнить значение; это необязательно.` : observed ? 'Источник: ваши данные или этикетка продукта.' : 'Для этого нутриента данных пока нет. Нулевое значение вводите только если оно известно.';
      compositionRows.replaceChildren();
      for (const n of catalog) {
        const row = meal?.observations.find(o => o.nutrient_code === n.code);
        const amount = row && !row.stale && row.value !== null ? Number(row.value.toFixed(5)).toLocaleString('ru-RU') : 'нет данных';
        node('p', `${n.name}: ${amount}${amount === 'нет данных' ? '' : ' ' + (n.unit === 'ug' ? 'мкг' : n.unit === 'mg' ? 'мг' : 'г')}`, compositionRows);
      }
    }
    async function load() {
      message.textContent = 'Загрузка…';
      try { const [c, m] = await Promise.all([api('catalog'), api('meals')]); catalog = c.nutrients; meals = m.items; mealSelect.replaceChildren(); nutrientSelect.replaceChildren(); for (const meal of meals) { const o = node('option', `${meal.date} · ${meal.title}`, mealSelect); o.value = String(meal.id); } for (const n of catalog) { const o = node('option', n.name, nutrientSelect); o.value = n.code; } form.hidden = !meals.length; message.textContent = meals.length ? 'Показаны последние 30 записей. Новые значения попадут в общий контекст примерно за 30 секунд.' : 'Сначала добавьте приём пищи в Food.'; fill(); } catch (e) { message.textContent = e.message; }
    }
    mealSelect.addEventListener('change', fill); nutrientSelect.addEventListener('change', fill);
    details.addEventListener('toggle', () => { if (details.open) load(); });
    form.addEventListener('submit', async event => { event.preventDefault(); const meal = selected(); if (!meal) return; const observed = meal.observations.find(o => o.nutrient_code === nutrientSelect.value); const raw = value.value.trim(); const amount = raw === '' ? null : Number(raw.replace(',', '.')); if (amount !== null && (!Number.isFinite(amount) || amount < 0)) { message.textContent = 'Введите неотрицательное число.'; return; } save.disabled = true; const chosen = {meal: mealSelect.value, nutrient: nutrientSelect.value}; try { await api(`meals/${meal.id}/${nutrientSelect.value}`, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({value:amount,unit:catalog.find(n => n.code === nutrientSelect.value).unit,expected_meal_version:meal.version,expected_revision:observed?.revision || 0,source:origin.value,reference:reference.value})}); await load(); mealSelect.value = chosen.meal; nutrientSelect.value = chosen.nutrient; fill(); message.textContent = 'Сохранено. Общий контекст обновится примерно за 30 секунд.'; } catch (e) { message.textContent = e.message; } finally { save.disabled = false; } });
    root.append(details);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount, {once:true}); else mount();
})();
