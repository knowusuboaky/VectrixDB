/* Light unless this browser chose dark, set before anything paints so nothing flashes the wrong theme.
   A file of its own, not inline: the page's policy runs no inline script. */
try { var t = localStorage.getItem('vectrixdb.theme'); if (t === 'dark' || t === 'light') document.documentElement.setAttribute('data-theme', t); } catch (e) {}
