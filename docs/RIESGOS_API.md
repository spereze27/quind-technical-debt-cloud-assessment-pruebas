# Riesgos de la implementación como API

Ordenados por impacto. «Hoy» = lo que ya hace el código; «Falta» = lo que debe resolverse antes de producción.

## Críticos

| # | Riesgo | Por qué ocurre en este proyecto | Hoy | Falta |
|---|---|---|---|---|
| 1 | **Los trabajos se pierden o se congelan según dónde se despliegue** | La auditoría corre como tarea de fondo del proceso (`asyncio`). En **Cloud Run** con la configuración por defecto la CPU solo existe mientras hay una petición activa: al responder `202` la tarea queda estrangulada. Con varias instancias o `--workers N`, el `GET` puede caer en un proceso que no conoce el trabajo. Al escalar a cero, todo se pierde | `JOB_STORE=sqlite` conserva el resultado tras reinicios y marca como interrumpidos los trabajos activos | Un solo proceso (`--workers 1`) y, en Cloud Run, `--no-cpu-throttling --min-instances=1 --max-instances=1`. Para escalar de verdad: cola externa (Cloud Tasks, Pub/Sub, Celery) y almacén compartido (Firestore, Postgres, Redis) |
| 2 | **Datos sensibles de clientes viajan a un LLM de terceros y a la respuesta** | El perfilado calcula mínimo, máximo, moda y valor dominante de columnas reales (identificaciones, NIT, montos). Esos valores se incluyen en el JSON, en el Excel y en los *prompts* del analizador de calidad | La clave del LLM y las credenciales nunca se registran ni se devuelven | Opción para **omitir valores** (solo porcentajes y conteos) y enmascarar columnas con PII antes de enviarlas al LLM; acuerdo de tratamiento de datos con el proveedor del LLM |
| 3 | **Inyección de instrucciones (*prompt injection*) desde la nube auditada** | Nombres de recursos, etiquetas, descripciones del diccionario y comentarios de columnas son texto controlado por terceros y entran al *prompt*. Pueden inducir hallazgos falsos o informes engañosos | El guardrail descarta reglas fuera del catálogo y recursos inexistentes; los hallazgos de calidad son deterministas | Los informes en Markdown (`final_report`, `data_report`, `dictionary_report`) **no se validan**: pueden contener cifras o conclusiones incorrectas. El frontend debe renderizarlos sin HTML crudo (Mermaid en modo estricto) y mostrar los números desde `kpis`, no desde el texto |
| 4 | **Credenciales y resultados sin cifrar durante el tránsito y en reposo** | `cloud_credentials` (clave privada de la Service Account) viaja en el cuerpo del `POST`; el resultado contiene todo el inventario (IAM, firewalls, cuentas de servicio) y se guarda en SQLite y en Excel temporales | Archivo temporal de credenciales con permisos `0600` que se borra siempre; los huérfanos se limpian al arrancar; SQLite en `0600`; los errores de validación no repiten el valor enviado | TLS obligatorio delante de la API; disco cifrado; no registrar cuerpos en el proxy; usar *Workload Identity* en vez de subir claves; vencimiento corto de trabajos y Excel |
| 5 | **La autenticación es una clave estática compartida** | `X-API-Key` identifica a una aplicación, no a una persona; rotarla exige redesplegar; quien la tenga puede consumir créditos de cómputo y generar costos de BigQuery | Comparación en tiempo constante; cada trabajo es visible solo para la clave que lo creó; límite de solicitudes por clave; `REQUIRE_AUTH=1` impide arrancar sin claves | Gateway con OAuth2/OIDC o IAP, identidad por usuario, rotación y revocación; el límite por minuto es por proceso |

## Altos

| # | Riesgo | Por qué ocurre | Hoy | Falta |
|---|---|---|---|---|
| 6 | **Cancelar y el tiempo máximo no detienen el trabajo real** | Los SDK de GCP y los nodos son síncronos y corren en hilos; `asyncio` solo deja de esperar. Las consultas a GCP y las llamadas al LLM (que cuestan) continúan hasta terminar el nodo | El estado pasa a `cancelled` o `error` y se libera el cupo de la cola | Los hilos de nodos cancelados siguen ocupando el *pool*; con muchos cancelados se degrada el servicio. Solución real: ejecutar cada auditoría en un proceso o contenedor aparte que sí se pueda terminar |
| 7 | **Costo y abuso del LLM y de BigQuery** | Cada auditoría hace unas 8 llamadas al LLM con *prompts* de hasta 180 000 caracteres; el perfilado lee tablas del cliente | Cola con concurrencia máxima (2), tope de auditorías en cola (20), límite por minuto, `profile_max_gb` y `maximum_bytes_billed` por consulta, tiempo máximo (30 min) | Presupuesto y alertas por proyecto y por clave; los topes de la cola son por proceso |
| 8 | **El contrato puede romperse sin que nadie lo note** | Los modelos de salida son permisivos a propósito (`extra=allow`, `kpis`, `data_profile` y `raw_infra_data` como diccionarios libres) para que un cambio interno no convierta una auditoría terminada en error `500` | `/openapi.json` documenta las rutas, la entrada completa y los campos principales de la salida | Esquemas estrictos y versionados para `kpis` y `data_profile`; pruebas de contrato en el pipeline (por ejemplo con Schemathesis) y política de versiones (`/api/v2`) antes de cambios incompatibles |
| 9 | **Respuestas muy pesadas** | `raw_infra_data` puede pesar varios MB y se valida y serializa en cada consulta | `?include_raw=false` lo omite; el sondeo no devuelve el resultado hasta que termina | Cada `GET` de un trabajo terminado vuelve a validar y serializar todo en el bucle de eventos; con SQLite se lee y decodifica el JSON completo cada vez. Conviene guardar el JSON ya serializado, comprimir (gzip) y ofrecer el inventario como descarga aparte |
| 10 | **Este código base no incluye los módulos de v1.5** | La reestructuración se hizo sobre su zip v1.4, que no tiene el diccionario ni el perfilado | La API acepta los campos de v1.5, y devuelve `501` si se envía un diccionario sin el módulo; `data_profile` queda en `null` | Copiar al proyecto los módulos de v1.5 (`core/dictionary.py`, `core/profiler.py`, nodos de perfilado y calidad, `quality_findings`, `profile_excel`, `profile_report`, y los cambios en `graph.py`, `state.py`, `kpi_engine.py` y `excel_export.py`). La capa API los detecta sola |

## Medios

| # | Riesgo | Detalle y qué falta |
|---|---|---|
| 11 | **Conexiones largas** | `POST /audit` y `/audit/stream` mantienen la conexión hasta 30 minutos; balanceadores y proxies suelen cortar a los 30–60 s. Usar siempre el flujo asíncrono con sondeo |
| 12 | **Sin observabilidad** | Hay `X-Request-ID` en cada respuesta y errores con ese identificador, pero no hay métricas, trazas ni alertas. Agregar Prometheus u OpenTelemetry (duración por nodo, fallos del LLM, permisos que faltan, cola) |
| 13 | **Permisos de la Service Account** | Si falta un rol, esa sección queda vacía y la auditoría termina `completed` con `discovery_status` `partial`. Un cliente que ignore `errors` podría interpretar «0 hallazgos» como «arquitectura sana» |
| 14 | **Validación solo contra simulaciones** | Las pruebas usan un LLM falso y clientes de GCP simulados. No se ha ejecutado contra una organización real ni con volúmenes grandes (cientos de proyectos, miles de tablas). Probar primero con un proyecto pequeño |
| 15 | **Dependencias sin versión fija** | `requirements.txt` solo declara mínimos; LangGraph y los SDK de LLM cambian con frecuencia. Generar un archivo de versiones fijas (`pip-compile` o `uv lock`) y probar las actualizaciones |
| 16 | **Entrada estricta** | La solicitud rechaza campos desconocidos (`422`). Evita errores de digitación, pero un cliente que agregue campos nuevos fallará |
| 17 | **Webhooks no implementados** | Notificar al terminar exigiría validar destinos contra SSRF (la API podría usarse para llamar a servicios internos). Hoy solo hay sondeo |


## Riesgos añadidos en la v1.7 (dominios y Cloud Run)

| # | Riesgo | Impacto | Mitigación |
|---|---|---|---|
| 18 | Cloud Run reinicia/redespliega la única instancia | Se pierden trabajos en curso y Excel | Cliente reintenta tras 404; para producción, cola y almacén externos |
| 19 | CPU throttling activado por error | Los análisis se congelan fuera de una solicitud | `--no-cpu-throttling` (ya en el workflow) |
| 20 | Servicio público + `ADC_ENABLED` | Quien tenga una API key audita con la identidad de `tech-debt-runtime` | Claves largas rotadas en Secret Manager, `run.invoker` si se necesita cierre de red, roles solo lectura |
| 21 | Arquitectura sin descubrimiento real | El modo mock y AWS/Azure no traen grafo: el análisis sale vacío | Usar GCP con `roles/cloudasset.viewer` |
| 22 | Reglas ARCH_* basadas en heurísticas (relaciones inferidas por nombres) | Falsos positivos de dependencias | Se marcan `inferred` y se reportan como severidad LOW |
| 23 | Clave de LLM inválida no se detecta al crear el trabajo | Resultado `completed` con avisos y sin hallazgos | Revisar `errors`; el smoke test lo muestra |
| 24 | Dominio `data-governance` sin módulos v1.5 | Diccionario → 501, `data_profile` nulo | Copiar `dictionary.py` y módulos de perfilado de la v1.5 |
