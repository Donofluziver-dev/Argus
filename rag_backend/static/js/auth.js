// ===== Anmeldung, Rollen, Kontoverwaltung =====
// Die Sitzung liegt in einem HttpOnly-Cookie: JavaScript kommt nicht daran, ein
// eingeschleustes Skript kann sie also nicht auslesen. Der Zustand hier ist nur
// eine Kopie fuer die Anzeige -- entschieden wird serverseitig an jeder Route.
window.argusSession = { authenticated: false, username: null, role: null, userId: null };

(function () {
  let mode = 'login';            // 'login' | 'setup' (erstes Konto) | 'register'
  let booted = false;
  let allowRegister = false;
  let setupTokenRequired = false;   // Erstlauf verlangt DASHBOARD_SETUP_TOKEN

  function el(id) { return document.getElementById(id); }

  function setMessage(text, ok) {
    const box = el('auth-msg');
    if (!box) return;
    box.textContent = text || '';
    box.classList.toggle('is-ok', !!ok);
  }

  // Der Punkt sagt nur eine Sache: hat das Backend geantwortet. Ueber SGLang,
  // Qdrant und TTS ist vor der Anmeldung nichts bekannt -- die Zeile daneben
  // zaehlt deshalb auf, was laeuft, ohne einen Zustand zu behaupten.
  function dienst(erreichbar) {
    const box = el('auth-dienst');
    if (!box) return;
    box.className = 'auth-dienst ' + (erreichbar ? 'ist-da' : 'ist-weg');
    box.innerHTML = '<i></i>' + (erreichbar ? 'DIENST ERREICHBAR' : 'DIENST NICHT ERREICHBAR');
  }

  function symbole() {
    if (!window.argusIcon) return;
    const logo = el('auth-logo');
    if (logo) logo.innerHTML = window.argusIcon('ring', 46);
    document.querySelectorAll('#auth-gate [data-icon]').forEach(function (n) {
      n.innerHTML = window.argusIcon(n.dataset.icon, 15);
    });
  }

  function applyMode(next) {
    mode = next;
    const isSetup = next === 'setup';
    const isRegister = next === 'register';
    const neuesKonto = isSetup || isRegister;

    el('auth-sub').textContent = isSetup ? 'ERSTES KONTO \u00b7 WIRD ADMINISTRATOR'
      : (isRegister ? 'NEUES KONTO \u00b7 BENUTZER'
                    : 'LOKALER AGENT \u00b7 ZUGANG ERFORDERLICH');
    el('auth-submit-text').textContent = neuesKonto ? 'Konto anlegen' : 'Anmelden';
    el('auth-confirm-row').style.display = neuesKonto ? '' : 'none';
    el('auth-token-row').style.display = (isSetup && setupTokenRequired) ? '' : 'none';
    // Beim Anlegen des ersten Kontos keine Dauer-Anmeldung anbieten -- das ist ein
    // einmaliger Vorgang, danach folgt ohnehin die normale Anmeldung.
    el('auth-remember-row').style.display = isSetup ? 'none' : '';
    el('auth-pass').setAttribute('autocomplete', neuesKonto ? 'new-password' : 'current-password');

    const umschalter = el('auth-switch');
    umschalter.hidden = isSetup || !allowRegister;
    umschalter.textContent = isRegister
      ? 'Zurück zur Anmeldung'
      : 'Noch kein Konto? Hier registrieren';

    setMessage(isSetup ? ('Noch kein Konto vorhanden. Dieses erste Konto wird Administrator.'
                          + (setupTokenRequired ? ' Der Einrichtungs-Schlüssel steht in der .env der Installation (DASHBOARD_SETUP_TOKEN).' : ''))
      : (isRegister ? 'Das Konto darf fragen, in den Dokumenten suchen und recherchieren.' : ''), neuesKonto);
  }

  // Zwei Zeichen fuer den Kreis in der Kopfzeile. Trennzeichen einer Mailadresse
  // oder eines Namens zaehlen als Wortgrenze; sonst die ersten beiden Buchstaben.
  function initialen(name) {
    const teile = String(name || '').split(/[.\-_@\s]+/).filter(Boolean);
    if (teile.length >= 2) return (teile[0][0] + teile[1][0]).toUpperCase();
    return String(name || '?').slice(0, 2).toUpperCase();
  }

  function applySession(data) {
    window.argusSession = {
      authenticated: true,
      username: data.username,
      role: data.role || 'user',
      userId: data.user_id
    };
    const admin = data.role === 'admin';
    document.body.classList.toggle('role-admin', admin);
    el('account-name').textContent = data.username;
    el('account-role').textContent = admin ? 'Administrator' : 'Benutzer';
    el('account-dot').textContent = initialen(data.username);
    el('account-dot').classList.toggle('is-admin', admin);
    // Rollengebundene Bedienelemente ausserhalb der Navigation (Datei-Knopf im Chat).
    document.querySelectorAll('[data-role="admin"]').forEach(function (node) {
      if (node.classList.contains('nav-link')) return;
      node.hidden = !admin;
    });
  }

  function openApp() {
    const gate = el('auth-gate');
    const shell = el('app-shell');
    shell.hidden = false;
    gate.classList.add('is-leaving');
    setTimeout(function () {
      gate.style.display = 'none';
      if (window.argusAuthOrb) window.argusAuthOrb.stop();
    }, 450);
    if (!booted) { booted = true; if (window.argusBoot) window.argusBoot(); }
    else if (window.argusRouteNow) window.argusRouteNow();
    if (window.argusOps) window.argusOps.heartbeat();
  }

  function showGate(reason) {
    const gate = el('auth-gate');
    el('app-shell').hidden = true;
    gate.style.display = '';
    gate.classList.remove('is-leaving');
    window.argusSession = { authenticated: false, username: null, role: null, userId: null };
    document.body.classList.remove('role-admin');
    if (window.argusAuthOrb) window.argusAuthOrb.start();
    if (window.argusOps) window.argusOps.halt();
    if (window.vcOrb) window.vcOrb.stop();
    el('auth-pass').value = '';
    closePassword();
    if (reason) setMessage(reason);
  }

  async function submit(ev) {
    ev.preventDefault();
    const btn = el('auth-submit');
    const username = el('auth-user').value.trim();
    const password = el('auth-pass').value;
    if (!username || !password) { setMessage('Benutzername und Passwort ausfüllen.'); return; }
    if (mode === 'setup' || mode === 'register') {
      if (username.length < 3) { setMessage('Benutzername mindestens 3 Zeichen.'); return; }
      if (password.length < 8) { setMessage('Passwort mindestens 8 Zeichen.'); return; }
      if (password !== el('auth-pass2').value) { setMessage('Die Passwörter stimmen nicht überein.'); return; }
    }
    btn.disabled = true;
    setMessage('');
    try {
      const PFADE = {
        setup: '/v1/dashboard/auth/setup',
        register: '/v1/dashboard/auth/register',
        login: '/v1/dashboard/auth/login'
      };
      // Die Rolle wird NIE mitgeschickt -- sie steht serverseitig fest (setup: admin,
      // register: chat). Ein Feld hier wäre nur eine Einladung, es zu verbiegen.
      const body = { username: username, password: password };
      if (mode !== 'setup') body.remember = el('auth-remember').checked;
      const war = mode;
      const opts = { method: 'POST', headers: { 'Content-Type': 'application/json' },
                     body: JSON.stringify(body) };
      // Der Schluessel geht als Header mit, nicht im Koerper: so prueft ihn das
      // Backend, bevor es die Eingaben ansieht.
      if (mode === 'setup' && setupTokenRequired) {
        opts.headers['X-Setup-Token'] = el('auth-token').value.trim();
      }
      const res = await api(PFADE[mode], opts);
      el('auth-token').value = '';
      applySession(res);
      // Ab jetzt gibt es ein Konto: nach dem Abmelden steht hier die Anmeldung,
      // nicht noch einmal die Einrichtung des ersten Kontos.
      if (war === 'setup') applyMode('login');
      el('auth-pass').value = '';
      if (el('auth-pass2')) el('auth-pass2').value = '';
      openApp();
    } catch (e) {
      setMessage((e && e.message) || 'Anmeldung fehlgeschlagen.');
    } finally {
      btn.disabled = false;
    }
  }

  async function logout() {
    try { await apiPost('/v1/dashboard/auth/logout'); } catch (e) { /* Cookie ist so oder so weg */ }
    showGate('Abgemeldet.');
  }

  // Eigener Dialog statt prompt(): das Backend verlangt das aktuelle Passwort, und
  // prompt() zeigte beide Eingaben im Klartext.
  function pwMessage(text, ok) {
    const box = el('pw-msg');
    box.textContent = text || '';
    box.classList.toggle('is-ok', !!ok);
  }

  function closePassword() {
    el('pw-modal').hidden = true;
    el('pw-form').reset();
  }

  function changePassword() {
    el('pw-form').reset();
    pwMessage('');
    el('account-menu').hidden = true;
    el('pw-modal').hidden = false;
    el('pw-current').focus();
  }

  async function submitPassword(ev) {
    ev.preventDefault();
    const neu = el('pw-new').value;
    if (neu.length < 8) { pwMessage('Passwort mindestens 8 Zeichen.'); return; }
    if (neu !== el('pw-new2').value) { pwMessage('Die neuen Passwörter stimmen nicht überein.'); return; }
    const btn = el('pw-submit');
    btn.disabled = true;
    try {
      await apiPost('/v1/dashboard/auth/password',
                    { password: neu, current_password: el('pw-current').value });
      el('pw-form').reset();
      pwMessage('Passwort geändert. Andere Sitzungen dieses Kontos sind beendet.', true);
      setTimeout(closePassword, 1800);
    } catch (e) {
      pwMessage((e && e.message) || 'Fehlgeschlagen.');
    } finally {
      btn.disabled = false;
    }
  }

  function bindAccountMenu() {
    const btn = el('account-btn'), menu = el('account-menu');
    if (!btn || !menu) return;
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      const open = menu.hidden;
      menu.hidden = !open;
      btn.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('click', function () {
      menu.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
    });
    menu.addEventListener('click', function (ev) { ev.stopPropagation(); });
    el('account-logout').addEventListener('click', logout);
    el('account-password').addEventListener('click', changePassword);
    el('pw-form').addEventListener('submit', submitPassword);
    el('pw-modal-close').addEventListener('click', closePassword);
    el('pw-modal-backdrop').addEventListener('click', closePassword);
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && !el('pw-modal').hidden) closePassword();
    });
  }

  async function init() {
    bindAccountMenu();
    el('auth-form').addEventListener('submit', submit);
    el('auth-switch').addEventListener('click', function () {
      applyMode(mode === 'register' ? 'login' : 'register');
      el('auth-user').focus();
    });
    if (window.argusAuthOrb) window.argusAuthOrb.start();
    symbole();
    try {
      const state = await api('/v1/dashboard/auth/state');
      dienst(true);
      if (state.authenticated) { applySession(state); openApp(); return; }
      allowRegister = !!state.allow_register;
      setupTokenRequired = !!state.setup_token_required;
      applyMode(state.needs_setup ? 'setup' : 'login');
    } catch (e) {
      // Backend nicht erreichbar: Anmeldemaske stehen lassen, nichts vortäuschen.
      dienst(false);
      applyMode('login');
      setMessage('Backend nicht erreichbar.');
    }
    el('auth-user').focus();
  }

  window.argusAuth = {
    // Von api() gerufen, sobald irgendein Aufruf 401 liefert.
    sessionLost: function () {
      if (!window.argusSession.authenticated) return;
      showGate('Sitzung abgelaufen — bitte erneut anmelden.');
    },
    logout: logout,
    isAdmin: function () { return window.argusSession.role === 'admin'; }
  };

  if (document.readyState !== 'loading') init();
  else document.addEventListener('DOMContentLoaded', init);
})();

// ===== Kontoverwaltung (Monitor-Ansicht, nur Administratoren) =====
(function () {
  let bound = false;

  function msg(text, ok) {
    const box = $('users-msg');
    if (!box) return;
    box.textContent = text || '';
    box.style.color = ok ? 'var(--color-green)' : 'var(--color-magenta)';
  }

  async function render() {
    const box = $('users-list');
    if (!box) return;
    try {
      const res = await api('/v1/dashboard/users');
      const users = res.users || [];
      box.innerHTML = users.map(function (u) {
        const self = u.is_self;
        const admin = u.role === 'admin';
        const zuletzt = u.last_active
          ? 'zuletzt ' + tcell(u.last_active)
          : 'noch nicht benutzt';
        // Der Farbverlauf des Punktes trennt Administratoren von Chatkonten auf
        // einen Blick -- dieselbe Aussage wie die Rollenbeschriftung daneben.
        return "<div class='user-row" + (self ? " is-self" : "") + "'>" +
          "<span class='user-avatar" + (admin ? " is-admin" : "") + "'></span>" +
          "<span class='user-name-line'>" +
            "<span class='user-name'>" + escapeHtml(u.username) +
              (self ? " <span class='muted' style=\"font-size:11px;\">(du)</span>" : "") + "</span>" +
            "<span class='user-last'>" + zuletzt + " · " + u.chats
              + (u.chats === 1 ? " Chat · " : " Chats · ") + u.messages
              + (u.messages === 1 ? " Nachricht" : " Nachrichten") + "</span>" +
          "</span>" +
          "<button class='user-badge" + (admin ? " is-admin" : "") + "' data-role-id='" + u.id +
            "' data-role='" + (admin ? "user" : "admin") + "' title='Rolle umschalten'>" +
            (admin ? "ADMINISTRATOR" : "BENUTZER") + "</button>" +
          (self
            ? "<span class='user-eigen'>EIGENES KONTO</span>"
            : "<button class='user-del' type='button' data-del-id='" + u.id + "' data-name='"
              + escapeHtml(u.username) + "' title='Konto löschen'>"
              + (window.argusIcon ? window.argusIcon('trash', 12) : '') + "ENTFERNEN</button>") +
          "</div>";
      }).join('') || '<div class="muted">Keine Konten.</div>';

      box.querySelectorAll('[data-role-id]').forEach(function (b) {
        b.addEventListener('click', function () { setRole(b.dataset.roleId, b.dataset.role); });
      });
      box.querySelectorAll('[data-del-id]').forEach(function (b) {
        b.addEventListener('click', function () { remove(b.dataset.delId, b.dataset.name); });
      });
    } catch (e) {
      box.innerHTML = '<div class="muted" style="color:var(--color-magenta);">Konten nicht ladbar.</div>';
    }
  }

  async function setRole(id, role) {
    try {
      await apiPost('/v1/dashboard/users/' + id + '/role?role=' + encodeURIComponent(role));
      msg('Rolle geändert.', true);
      render();
    } catch (e) { msg((e && e.message) || 'Fehlgeschlagen.'); }
  }

  async function remove(id, name) {
    // Mit dem Konto gehen seine Chatverläufe -- das steht in der Rückfrage, weil es
    // sich nicht rückgängig machen lässt.
    if (!confirm('Konto "' + name + '" mitsamt allen Chatverläufen löschen?')) return;
    try {
      const res = await api('/v1/dashboard/users/' + id, { method: 'DELETE' });
      msg('Konto gelöscht (' + res.chats_removed + ' Chats entfernt).', true);
      render();
    } catch (e) { msg((e && e.message) || 'Fehlgeschlagen.'); }
  }

  async function create(ev) {
    ev.preventDefault();
    const name = $('user-new-name').value.trim();
    const pass = $('user-new-pass').value;
    const role = $('user-new-role').value;
    if (name.length < 3 || pass.length < 8) {
      msg('Benutzername ab 3, Passwort ab 8 Zeichen.');
      return;
    }
    try {
      await apiPost('/v1/dashboard/users', { username: name, password: pass, role: role });
      $('user-new-name').value = '';
      $('user-new-pass').value = '';
      msg('Konto "' + name + '" angelegt.', true);
      render();
    } catch (e) { msg((e && e.message) || 'Fehlgeschlagen.'); }
  }

  window.argusUsers = {
    refresh: function () {
      if (!bound) {
        const form = $('users-add');
        if (form) { form.addEventListener('submit', create); bound = true; }
      }
      render();
    }
  };
})();
