(() => {
  const name = document.getElementById('account-name');
  const results = document.getElementById('account-search-results');
  let timer, controller;
  if (name) {
    const search = () => {
      clearTimeout(timer);
      controller?.abort();
      results.replaceChildren();
      if (name.value.trim().length < 3) return;
      const query = name.value.trim();
      timer = setTimeout(async () => {
        controller = new AbortController();
        try {
          const response = await fetch('/cs/cuentas/buscar?q=' + encodeURIComponent(query), {signal:controller.signal});
          if (!response.ok) throw new Error('No pudimos consultar las cuentas. Se verificará nuevamente al guardar.');
          const hits = await response.json();
          if (name.value.trim() !== query) return;
          for (const hit of hits) {
            const row = document.createElement('div'); row.className = 'daily-search-hit';
            const title = document.createElement('strong');title.textContent=hit.name;
            const detail = document.createElement('p');detail.className='weekly-note';detail.textContent='Responsable: '+hit.kam;
            row.append(title,detail);
            if (hit.url) {const link=document.createElement('a');link.className='btn';link.href=hit.url;link.textContent='Abrir cuenta existente';row.append(link);}
            else {const note=document.createElement('p');note.className='weekly-note';note.textContent='Solicita a Dirección revisar la asignación de esta cuenta.';row.append(note);}
            results.append(row);
          }
        } catch (e) {if(e.name!=='AbortError')results.textContent=e.message;}
      },300);
    };
    name.addEventListener('input',search);search();
  }
  const source=document.querySelector('[name="source"]');
  const toggle = () => document.querySelectorAll('[data-source-extra]').forEach(el => {
    el.hidden=el.dataset.sourceExtra!==source?.value;
    el.querySelectorAll('[data-required]').forEach(input => {input.required=!el.hidden;input.disabled=el.hidden;});
  });
  source?.addEventListener('change',toggle);toggle();
  document.querySelectorAll('form[data-save],#add-account-form').forEach(form => form.addEventListener('submit',()=>{
    form.querySelectorAll('button[type="submit"]').forEach(button => {button.disabled=true;button.textContent='Guardando…';});
  }));
})();
