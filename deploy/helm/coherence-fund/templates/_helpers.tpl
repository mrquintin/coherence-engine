{{- define "coherence-fund.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "coherence-fund.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "coherence-fund.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
  Base labels merged into each PrometheusRule alert (routing hooks for Alertmanager).
*/}}
{{- define "coherence-fund.prometheusRuleAlertLabels" -}}
severity: {{ .Values.prometheusRules.alertSeverity | default "warning" | quote }}
team: {{ .Values.prometheusRules.alertTeam | default "platform" | quote }}
service: "coherence-fund"
{{- range $k, $v := (.Values.prometheusRules.extraAlertLabels | default dict) }}
{{ $k }}: {{ $v | quote }}
{{- end }}
{{- end -}}

