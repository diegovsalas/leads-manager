# Avisos por asignación de leads

Cada alta de lead con vendedor asignado y cada cambio real de vendedor genera
un aviso después de confirmar la transacción. No se avisa al editar notas,
repetir el mismo propietario ni desasignar. No se generan avisos retroactivos.

El destinatario es el correo de la única cuenta activa de `users_crm` vinculada
al vendedor. Si falta el correo o hay varias cuentas activas con correos distintos,
el aviso queda pendiente; no se adivina un destinatario. No hay CC ni BCC.

El mensaje sale de `CRM Avantex <crm@grupoavantex.com>` e incluye nombre del lead,
empresa, marca, fecha de asignación en Monterrey y un enlace al CRM que conserva
el destino al iniciar sesión. Teléfono, correo del cliente y notas permanecen
únicamente en el CRM. El enlace exige una sesión con permisos sobre el lead.

## Envío y reintentos

La tabla `lead_assignment_notices` se escribe en la misma transacción que el
lead. Un rollback descarta el aviso. El envío corre en segundo plano después del
commit y no afecta la asignación si Resend falla. Requiere `RESEND_API_KEY` y el
dominio remitente configurado en Resend; `DOMAIN` define la URL del CRM.

Las reservas de dos minutos evitan procesar un aviso a la vez en dos workers.
La petición se persiste antes de enviarla, junto con una clave idempotente por
aviso. Los reintentos conservan exactamente la misma petición. Antes de enviarla
se comprueba que el vendedor sigue siendo el asignado y que no hay una asignación
más reciente; los avisos superados se omiten.

El scheduler interno reintenta cada minuto (`avisos-asignacion`). En modo externo,
se puede ejecutar `/tareas/avisos-asignacion` con la autenticación de tareas existente.
Las peticiones al CRM también activan el procesamiento de pendientes, como máximo
una vez por minuto, de modo que los cron externos existentes siguen despertando
la cola. Sin tráfico ni scheduler no se ejecutan reintentos hasta la siguiente
activación; los avisos permanecen guardados.

Resend conserva sus claves durante 24 horas. Si un aviso cuyo envío ya se intentó
lleva 23 horas sin confirmación, pasa a `revision` para evitar duplicarlo después
de una interrupción prolongada. No se reenvía automáticamente fuera de esa ventana.

Los administradores pueden consultar `/api/leads/avisos-asignacion/estado` para
ver si está configurado el correo y los totales por estado, sin ejecutar envíos
ni exponer credenciales.

## Pruebas

`python3 -m pytest -q tests/test_lead_notifications.py` utiliza un proveedor
simulado. No crea leads ni manda mensajes de prueba en producción.
