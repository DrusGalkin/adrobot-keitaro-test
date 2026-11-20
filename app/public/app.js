const $ = selector => document.querySelector(selector);
const names = new Map();
let selection = 0;
let catalogLoaded = false;

async function api(path, options = {}) {
  const response = await fetch('/api/' + path, {
    ...options,
    headers: options.body ? {'Content-Type': 'application/json'} : {},
  });
  const data = await response.json();
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map(item => item.msg.replace(/^Value error, /, '')).join(' ') : data.detail;
    throw new Error(detail || 'Запрос завершился ошибкой.');
  }
  return data;
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function status(element, text = '', error = false) {
  element.textContent = text;
  element.classList.toggle('error', error);
}

function labelOffer(offer) {
  return `#${offer.id} · ${offer.name}`;
}

function offerPicker(input, list, errorBox) {
  let revision = 0;
  let timer;
  async function lookup() {
    const requested = ++revision;
    try {
      const offers = await api('offers?q=' + encodeURIComponent(input.value));
      if (requested !== revision) return;
      for (const offer of offers) names.set(offer.id, offer.name);
      list.replaceChildren(...offers.map(offer => new Option(labelOffer(offer))));
    } catch (error) {
      if (requested === revision) status(errorBox, error.message, true);
    }
  }
  input.addEventListener('focus', lookup, {once:true});
  input.addEventListener('input', () => {
    revision++;
    clearTimeout(timer);
    if (!/^#\d+ · /.test(input.value)) timer = setTimeout(lookup, 170);
  });
  return () => {
    const match = input.value.trim().match(/^(?:#(\d+) · .+|(\d+))$/);
    const id = Number(match?.[1] || match?.[2]);
    if (!Number.isSafeInteger(id) || id < 1) throw new Error('Выберите оффер из подсказок или введите его ID.');
    return id;
  };
}

function showTab(editing) {
  $('#create-panel').hidden = editing;
  $('#edit-panel').hidden = !editing;
  $('#create-tab').setAttribute('aria-pressed', !editing);
  $('#edit-tab').setAttribute('aria-pressed', editing);
}

const createForm = $('#create-form');
const createStatus = $('#create-status');
const editStatus = $('#edit-status');
const createOffer = offerPicker($('#create-offer'), $('#create-options'), createStatus);
const campaignList = $('#campaign-list');

async function loadCatalog() {
  try {
    const data = await api('catalog');
    $('#mode').textContent = data.mode === 'demo' ? 'Деморежим' : 'Keitaro подключён';
    createForm.elements.name.placeholder = `${data.campaign_prefix}campaign1`;
    for (const [name, items] of Object.entries({
      domain_id: data.domains, group_id: data.groups, traffic_source_id: data.sources,
    })) {
      createForm.elements[name].replaceChildren(...items.map(item => new Option(item.name, item.id)));
    }
    catalogLoaded = [data.domains, data.groups, data.sources].every(items => items.length);
    createForm.querySelector('button').disabled = !catalogLoaded;
    if (!catalogLoaded) status(createStatus, 'В Keitaro нужны домен, группа кампаний и источник трафика.', true);
  } catch (error) {
    status(createStatus, error.message, true);
  }
}

let listRequest = 0;
async function listCampaigns(active = campaignList.value) {
  const generation = ++listRequest;
  try {
    const items = await api('campaigns?q=' + encodeURIComponent($('#campaign-search').value));
    if (generation !== listRequest) return;
    campaignList.replaceChildren(new Option(items.length ? 'Выберите кампанию' : 'Кампаний нет', ''),
      ...items.map(c => new Option(`#${c.id} · ${c.name}`, c.id)));
    campaignList.value = active;
    if (!campaignList.value) campaignList.value = '';
  } catch (error) {
    if (generation === listRequest) status(editStatus, error.message, true);
  }
}

createForm.addEventListener('submit', async event => {
  event.preventDefault();
  const button = createForm.querySelector('button');
  button.disabled = true;
  status(createStatus, 'Создаём кампанию…');
  $('#created-link').replaceChildren();
  try {
    const body = Object.fromEntries(new FormData(createForm));
    body.offer_id = createOffer();
    const campaign = await api('campaigns', {method:'POST', body:JSON.stringify(body)});
    status(createStatus, `Кампания #${campaign.id} создана.`);
    const box = $('#created-link');
    box.append(node('p', campaign.url, 'muted'));
    const open = node('button', 'Редактировать офферы');
    open.addEventListener('click', async () => {
      showTab(true);
      $('#campaign-search').value = '';
      await listCampaigns(campaign.id);
      await openCampaign(campaign.id);
    });
    box.append(open);
  } catch (error) {
    status(createStatus, error.message, true);
  } finally {
    button.disabled = !catalogLoaded;
  }
});

function editor(stream, campaign) {
  const card = node('section', '', 'card');
  const message = node('p', '', 'status');
  message.setAttribute('role', 'status');
  const table = node('table');
  const header = table.createTHead().insertRow();
  for (const value of ['Оффер', 'Доля', '']) header.append(node('th', value));
  const rows = table.createTBody();
  const list = node('datalist');
  list.id = `options-${stream.id}`;
  const input = node('input');
  input.setAttribute('list', list.id);
  input.autocomplete = 'off';
  const label = node('label', 'Добавить оффер');
  label.append(input, list);
  const add = node('button', 'Добавить');
  const save = node('button', 'Сохранить', 'primary');
  const reset = node('button', 'Отменить правки');
  const search = node('div', '', 'search-row');
  search.append(label, add);
  const actions = node('div', '', 'actions');
  actions.append(save, reset);
  card.append(node('h2', stream.name), search, table, actions, message);
  const readId = offerPicker(input, list, message);
  let baseline = stream.offers;
  let draft = baseline.map(o => ({...o}));
  let version = stream.version;
  let busy = false;
  const changed = () => JSON.stringify(draft.map(o => o.offer_id)) !== JSON.stringify(baseline.map(o => o.offer_id));
  function render() {
    rows.replaceChildren();
    const active = draft.filter(o => o.state === 'active');
    draft.forEach(offer => {
      const row = rows.insertRow();
      const i = active.indexOf(offer);
      const share = changed() ? i < 0 ? 0 : Math.floor(100 / active.length) + Number(i < 100 % active.length) : offer.share;
      const name = offer.name || names.get(offer.offer_id);
      row.insertCell().textContent = `#${offer.offer_id}${name ? ' · ' + name : ''}${i < 0 ? ' · выключен' : ''}`;
      row.insertCell().textContent = `${share}%`;
      const remove = node('button', 'Удалить');
      remove.disabled = busy || (i >= 0 && active.length === 1);
      remove.addEventListener('click', () => { draft = draft.filter(item => item !== offer); status(message); render(); });
      row.insertCell().append(remove);
    });
    save.disabled = busy || !changed() || !active.length;
    reset.disabled = busy || !changed();
    add.disabled = busy || draft.length >= 100;
  }
  add.addEventListener('click', () => {
    try {
      const id = readId();
      if (draft.some(item => item.offer_id === id)) throw new Error('Этот оффер уже добавлен.');
      draft.push(baseline.find(o => o.offer_id === id) || {offer_id:id,state:'active'});
      input.value = '';
      status(message);
      render();
    } catch (error) { status(message, error.message, true); }
  });
  input.addEventListener('keydown', event => { if (event.key === 'Enter') add.click(); });
  reset.addEventListener('click', () => { draft = baseline.map(o => ({...o})); status(message); render(); });
  save.addEventListener('click', async () => {
    busy = true;
    render();
    status(message, 'Сохраняем…');
    try {
      const body = JSON.stringify({offer_ids:draft.map(o => o.offer_id),version});
      const data = await api(`campaigns/${campaign}/streams/${stream.id}/offers`, {method:'PUT',body});
      version = data.version;
      for (const offer of baseline) if (offer.name) names.set(offer.offer_id, offer.name);
      baseline = data.offers;
      draft = baseline.map(o => ({...o}));
      status(message, 'Изменения сохранены.');
    } catch (error) { status(message, error.message, true); }
    finally { busy = false; render(); }
  });
  render();
  return card;
}

async function openCampaign(identifier) {
  const token = ++selection;
  $('#flows').replaceChildren();
  status(editStatus, identifier ? 'Загружаем потоки…' : '');
  if (!identifier) return;
  try {
    const campaign = await api('campaigns/' + identifier);
    if (token !== selection) return;
    for (const stream of campaign.streams) {
      if (stream.editable) {
        $('#flows').append(editor(stream, campaign.id));
      } else {
        const card = node('section', '', 'card');
        card.append(node('h2', stream.name));
        const geo = stream.filters.find(filter => filter.name === 'country')?.payload;
        const route = typeof stream.destination === 'string' ? stream.destination : 'Поток без редактируемых офферов';
        card.append(node('p', (geo ? `${Array.isArray(geo) ? geo.join(', ') : geo} · ` : '') + route, 'muted'));
        $('#flows').append(card);
      }
    }
    status(editStatus, campaign.streams.length ? '' : 'В кампании нет потоков.');
  } catch (error) {
    if (token === selection) status(editStatus, error.message, true);
  }
}

$('#create-tab').onclick = () => showTab(false);
$('#edit-tab').onclick = () => { showTab(true); listCampaigns(); };
campaignList.onchange = () => openCampaign(campaignList.value);
let listTimer;
$('#campaign-search').oninput = () => {
  clearTimeout(listTimer);
  listTimer = setTimeout(() => listCampaigns(),170);
};
loadCatalog();
