{{- define "sgr.name" -}}
{{ .Chart.Name }}
{{- end }}

{{- define "sgr.fullname" -}}
{{ .Release.Name }}-{{ .Chart.Name }}
{{- end }}

{{- define "sgr.labels" -}}
helm.sh/chart: {{ include "sgr.chart" . }}
{{ include "sgr.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "sgr.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sgr.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "sgr.chart" -}}
{{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end }}
