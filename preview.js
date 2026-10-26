(() => {
  const toggle = document.getElementById('language-toggle');
  const translated = [...document.querySelectorAll('[data-fa]')].map(element => ({
    element, english: element.textContent, persian: element.dataset.fa
  }));
  const images = [...document.querySelectorAll('[data-fa-alt]')].map(element => ({
    element, english: element.alt, persian: element.dataset.faAlt
  }));
  let language = 'en';
  try {
    if (localStorage.getItem('modernsoft-language') === 'fa') language = 'fa';
  } catch {}

  function applyLanguage() {
    const persian = language === 'fa';
    document.documentElement.lang = language;
    document.documentElement.dir = persian ? 'rtl' : 'ltr';
    for (const entry of translated) entry.element.textContent = persian ? entry.persian : entry.english;
    for (const entry of images) entry.element.alt = persian ? entry.persian : entry.english;
    toggle.textContent = persian ? 'English' : 'فارسی';
    toggle.lang = persian ? 'en' : 'fa';
    toggle.setAttribute('aria-label', persian ? 'Switch to English' : 'تغییر زبان به فارسی');
    document.title = persian ? 'ModernSoft — نصب و مدیریت n8n' : 'ModernSoft — n8n Manager';
  }

  toggle.addEventListener('click', () => {
    language = language === 'en' ? 'fa' : 'en';
    applyLanguage();
    try {
      localStorage.setItem('modernsoft-language', language);
    } catch {}
  });
  applyLanguage();
})();
