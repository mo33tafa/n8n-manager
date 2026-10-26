const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'preview.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const decode = value => value.replaceAll('&amp;', '&');

function setup(stored = null, blocked = false) {
  const translated = [...html.matchAll(/<[^>]+data-fa="([^"]+)"[^>]*>([^<]*)<\//g)].map(match => ({
    dataset: { fa: decode(match[1]) }, textContent: decode(match[2])
  }));
  const images = [...html.matchAll(/<img[^>]+alt="([^"]+)"[^>]+data-fa-alt="([^"]+)"[^>]*>/g)].map(match => ({
    alt: decode(match[1]), dataset: { faAlt: decode(match[2]) }
  }));
  const initial = translated.map(element => element.textContent);
  const initialAlt = images.map(element => element.alt);
  const attributes = {};
  let handler;
  const button = {
    textContent: '', lang: '',
    setAttribute: (key, value) => { attributes[key] = value; },
    addEventListener: (event, callback) => { assert.equal(event, 'click'); handler = callback; }
  };
  const document = {
    documentElement: {}, title: '',
    getElementById: id => { assert.equal(id, 'language-toggle'); return button; },
    querySelectorAll: selector => selector === '[data-fa]' ? translated : images
  };
  const storage = new Map(stored === null ? [] : [['modernsoft-language', stored]]);
  const localStorage = {
    getItem: key => { if (blocked) throw new Error('Storage disabled'); return storage.get(key); },
    setItem: (key, value) => { if (blocked) throw new Error('Storage disabled'); storage.set(key, value); }
  };
  vm.runInNewContext(source, { document, localStorage });
  assert.ok(translated.length > 35, 'Translate all description sections');
  assert.equal(images.length, 3);
  return { document, button, translated, images, initial, initialAlt, storage, click: () => handler(), attributes };
}

test('English default, Persian RTL switch, and complete round-trip', () => {
  const app = setup();
  assert.equal(app.document.documentElement.lang, 'en');
  assert.equal(app.document.documentElement.dir, 'ltr');
  assert.deepEqual(app.translated.map(element => element.textContent), app.initial);
  app.click();
  assert.equal(app.document.documentElement.lang, 'fa');
  assert.equal(app.document.documentElement.dir, 'rtl');
  assert.equal(app.button.textContent, 'English');
  assert.equal(app.storage.get('modernsoft-language'), 'fa');
  for (const element of app.translated) assert.equal(element.textContent, element.dataset.fa);
  for (const image of app.images) assert.equal(image.alt, image.dataset.faAlt);
  app.click();
  assert.equal(app.document.documentElement.dir, 'ltr');
  assert.deepEqual(app.translated.map(element => element.textContent), app.initial);
  assert.deepEqual(app.images.map(element => element.alt), app.initialAlt);
  assert.equal(app.storage.get('modernsoft-language'), 'en');
});

test('Saved Persian preference is applied on load', () => {
  const app = setup('fa');
  assert.equal(app.document.documentElement.lang, 'fa');
  assert.equal(app.button.lang, 'en');
  assert.equal(app.attributes['aria-label'], 'Switch to English');
});

test('Disabled storage does not prevent switching', () => {
  const app = setup(null, true);
  app.click();
  assert.equal(app.document.documentElement.lang, 'fa');
  app.click();
  assert.equal(app.document.documentElement.lang, 'en');
});

test('Unknown saved values fall back to English', () => {
  assert.equal(setup('invalid').document.documentElement.lang, 'en');
});
