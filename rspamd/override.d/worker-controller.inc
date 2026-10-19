# Controller nur fürs Anlernen durch mailsweep (#12): im internen Docker-Netz und nur mit Passwort.
# Das Passwort kommt aus RSPAMD_CONTROLLER_PASSWORD (compose.yaml setzt es aus RSPAMD_PASSWORD).
# Ohne Passwort bleibt der Controller aus. Wichtig, weil das Ubuntu-Paket sonst das
# Standardpasswort "q1" mitbringt.
{% if env.CONTROLLER_PASSWORD %}
bind_socket = "*:11334";
count = 1;
password = "{= env.CONTROLLER_PASSWORD =}";
enable_password = "{= env.CONTROLLER_PASSWORD =}";
secure_ip = [];
{% else %}
enabled = false;
{% endif %}
