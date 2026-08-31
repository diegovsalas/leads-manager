# Tareas programadas

Las 13 tareas periódicas del CRM se pueden correr de dos formas. **Solo una
a la vez** — con las dos activas, cada tarea correría duplicada: dos
sincronizaciones de Savio, dos tandas de correos.

## Modo actual: scheduler interno

Es como funciona hoy. Un APScheduler dentro del proceso web dispara todo.

Funciona bien mientras el servicio esté siempre despierto, y es la razón
por la que corre con `-w 1`: dos workers duplicarían cada tarea.

## Modo plan gratuito: programador externo

En el plan gratuito de Render el servicio **se apaga tras 15 minutos sin
tráfico**. Un proceso dormido no consulta Meta, no sincroniza Savio y no
respalda nada — sin error, simplemente no ocurre.

### Cómo cambiar

1. En Render, agrega dos variables de entorno:

       SCHEDULER_EN_PROCESO = 0
       TAREAS_SECRET        = <una cadena larga y aleatoria>

2. Reinicia el servicio. En el log debe aparecer:

       Scheduler interno APAGADO — las tareas las dispara un programador externo

3. Da de alta los llamados en un programador externo gratuito
   (cron-job.org, EasyCron, GitHub Actions...). Cada uno es un GET a:

       https://TU-DOMINIO/tareas/<nombre>?secret=<TAREAS_SECRET>

### Qué programar

| Tarea | Cadencia | Qué hace |
|---|---|---|
| `meta-leads` | cada 5 min | Trae los leads nuevos de Meta Lead Ads |
| `linkedin-leads` | cada 5 min | Trae los leads nuevos de LinkedIn |
| `gmail-poll` | cada 5 min | Lee los correos nuevos de los vendedores |
| `cadencia` | cada 15 min | Seguimiento de leads sin respuesta |
| `savio-horario` | cada hora | Facturas y pagos de Savio |
| `kam-respuestas` | cada hora | Respuestas de clientes a los KAM |
| `savio-6h` | cada 6 horas | Suscripciones, clientes y MRR |
| `notificaciones` | 9:00 CST | Correo diario a cada vendedor |
| `backup` | 3:00 CST | Respaldo de la base |
| `gmail-purge` | 4:00 CST | Borra correos viejos |
| `zoho-citas` | 4:30 CST | Citas de Zoho Analytics |
| `sdr-engine` | 5:00 CST | Lote diario del SDR directivo |
| `savio-reconcilia` | semanal | Barrido completo, por si el incremental saltó algo |

Las horas son de México (UTC−6). La mayoría de programadores piden UTC:
súmale 6 horas.

Para ver el estado sin ejecutar nada:

    GET https://TU-DOMINIO/tareas/?secret=<TAREAS_SECRET>

### Efecto secundario útil

Las llamadas de cada 5 minutos **mantienen el servicio despierto**, así que
en la práctica casi nunca duerme. Eso también protege los webhooks.

## Lo que el plan gratuito NO resuelve

**Los webhooks siguen expuestos.** Meta, WhatsApp y Baileys mandan avisos a
`/webhook/...`. Si llegan con el servicio dormido, el primero espera el
arranque y el emisor corta antes. Meta reintenta un par de veces y descarta.

Las tareas de 5 minutos reducen mucho el riesgo, pero no lo eliminan: una
madrugada tranquila puede dejar una ventana.

**512 MB de RAM.** Esta aplicación carga gevent, SQLAlchemy con 65 modelos,
Socket.IO y una docena de integraciones. Puede quedar justo.

**Socket.IO.** El chat en tiempo real necesita conexión persistente. Cada
apagado la corta.

## Respuestas del endpoint

- `200 {"ok": true, ...}` — corrió bien
- `200 {"omitida": true}` — esa integración no está configurada; no es error
- `403` — secreto ausente o inválido
- `404` — no existe esa tarea
- `500` — la tarea falló. **A propósito**: así el programador externo lo
  detecta y avisa. Una tarea que falla en silencio es justo el problema que
  esto viene a resolver.
