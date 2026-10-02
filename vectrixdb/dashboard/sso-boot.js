/* Back from single sign-on: the page paints this at once, before the app has loaded, so there is no
   flash of anything else. The app takes over on the same picture and removes it. A file of its own, not
   inline: the page's policy runs no inline script. */
if (/[?&]sso=1\b/.test(location.search)) document.write('<div class="sso-screen" id="vx-boot"><div class="spin" aria-hidden="true"></div><h1>Checking your access<span class="dots" aria-hidden="true"><i>.</i><i>.</i><i>.</i></span></h1><p>Your company sign-in, your security group and the list of addresses allowed in.</p></div>');
