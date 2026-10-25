// Deliberately simple demo logic. Credentials are fake and checked client-side.
function login() {
  const f = document.getElementById('login-form'), err = document.querySelector('[data-testid=login-error]');
  f.addEventListener('submit', (e) => {
    e.preventDefault();
    const u = f.username.value.trim(), p = f.password.value;
    setTimeout(() => {
      if (u === 'demo' && p === 'demo-pass-123') { sessionStorage.setItem('user', u); location.href = 'board.html'; }
      else { err.textContent = 'Invalid username or password'; err.hidden = false; }
    }, 300);
  });
}
function board() {
  const user = sessionStorage.getItem('user');
  if (!user) { location.href = 'index.html'; return; }
  document.querySelector('[data-testid=welcome]').textContent = 'Welcome, ' + user;
  let tasks = [{ t: 'Review SRS', d: true }, { t: 'Update test plan', d: false }, { t: 'Run regression', d: false }];
  const list = document.querySelector('[data-testid=task-list]'), filter = document.getElementById('filter');
  function render() {
    list.innerHTML = '';
    tasks.forEach((x, i) => {
      if (filter.value === 'open' && x.d || filter.value === 'done' && !x.d) return;
      const li = document.createElement('li'); const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.checked = x.d; cb.setAttribute('aria-label', 'Done: ' + x.t);
      cb.onchange = () => { x.d = cb.checked; render(); };
      const s = document.createElement('span'); s.textContent = x.t; if (x.d) s.className = 'done';
      li.append(cb, s); list.append(li);
    });
    document.querySelector('[data-testid=remaining]').textContent = tasks.filter(x => !x.d).length + ' remaining';
  }
  document.getElementById('add-form').addEventListener('submit', (e) => {
    e.preventDefault(); const inp = document.querySelector('[data-testid=new-task]');
    if (inp.value.trim()) { tasks.push({ t: inp.value.trim(), d: false }); inp.value = ''; render(); }
  });
  filter.onchange = render;
  document.getElementById('logout').onclick = (e) => { e.preventDefault(); sessionStorage.clear(); location.href = 'index.html'; };
  render();
}
