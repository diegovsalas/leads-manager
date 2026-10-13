(() => {
  const form = document.getElementById('weekly-form');
  if (!form) return;
  form.addEventListener('invalid', event => {
    const section = event.target.closest('details');
    if (section) section.open = true;
  }, true);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    document.getElementById('weekly-action').value = event.submitter?.value || 'guardar';
    const payload = {metrics: Object.fromEntries(Array.from(form.querySelectorAll('[data-metric]')).map(input => [input.dataset.key,input.value]))};
    document.getElementById('weekly-payload').value = JSON.stringify(payload);
    const buttons = [...form.querySelectorAll('[type="submit"]')];
    const error = document.getElementById('weekly-error');
    buttons.forEach(button=>button.disabled=true);
    error.hidden = true;
    document.getElementById('weekly-save-status').textContent = 'Guardando…';
    try {
      const response = await fetch(location.href, {method: 'POST', body: new FormData(form), headers: {Accept: 'application/json'}});
      if (!response.ok) {
        const message = await response.text();
        let parsed; try { parsed = JSON.parse(message); } catch (_) {}
        throw new Error(parsed?.error || 'No se pudo guardar. Revisa los datos o recarga la página.');
      }
      location.assign(response.url);
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
      error.scrollIntoView({behavior:'smooth',block:'center'});
      document.getElementById('weekly-save-status').textContent = '';
      buttons.forEach(button=>button.disabled=false);
    }
  });
  document.getElementById('weekly-print').addEventListener('click', () => {
    const sections = [...document.querySelectorAll('.weekly-section')];
    const open = sections.map(section => section.open);
    sections.forEach(section => section.open = true);
    window.print();
    sections.forEach((section,i) => section.open = open[i]);
  });
})();
