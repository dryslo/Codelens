{{- define "codelens.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "codelens.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := include "codelens.name" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "codelens.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "codelens.labels" -}}
helm.sh/chart: {{ include "codelens.chart" . }}
{{ include "codelens.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "codelens.selectorLabels" -}}
app.kubernetes.io/name: {{ include "codelens.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "codelens.secretName" -}}
{{- printf "%s-secrets" (include "codelens.fullname" .) -}}
{{- end -}}

{{/* Полное имя образа компонента: registry/<name>:tag */}}
{{- define "codelens.image" -}}
{{- printf "%s/%s:%s" .root.Values.image.registry .name .root.Values.image.tag -}}
{{- end -}}

{{/*
Generic Deployment+Service[+HPA] для stateless-сервиса.
Вызов: {{ include "codelens.workload" (dict "root" . "name" "backend" "spec" .Values.backend) }}
*/}}
{{- define "codelens.workload" -}}
{{- $ := .root -}}
{{- $spec := .spec -}}
{{- $name := .name -}}
{{- $full := printf "%s-%s" (include "codelens.fullname" $) $name -}}
{{- if $spec.enabled }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $full }}
  labels:
    {{- include "codelens.labels" $ | nindent 4 }}
    app.kubernetes.io/component: {{ $name }}
spec:
  replicas: {{ $spec.replicas }}
  selector:
    matchLabels:
      {{- include "codelens.selectorLabels" $ | nindent 6 }}
      app.kubernetes.io/component: {{ $name }}
  template:
    metadata:
      labels:
        {{- include "codelens.selectorLabels" $ | nindent 8 }}
        app.kubernetes.io/component: {{ $name }}
    spec:
      {{- with $spec.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $spec.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $spec.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: {{ $name }}
          image: {{ include "codelens.image" (dict "root" $ "name" $spec.image) }}
          imagePullPolicy: {{ $.Values.image.pullPolicy }}
          ports:
            - containerPort: {{ $spec.port }}
          envFrom:
            - secretRef:
                name: {{ include "codelens.secretName" $ }}
          env:
            - name: CODELENS_CONFIG
              value: /app/config/config.yaml
            {{- range $spec.env }}
            - name: {{ .name }}
              value: {{ .value | quote }}
            {{- end }}
          {{- if $spec.healthPath }}
          readinessProbe:
            httpGet:
              path: {{ $spec.healthPath }}
              port: {{ $spec.port }}
            initialDelaySeconds: 10
            periodSeconds: 10
          {{- end }}
          {{- with $spec.resources }}
          resources:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          volumeMounts:
            - name: config
              mountPath: /app/config/config.yaml
              subPath: config.yaml
      volumes:
        - name: config
          configMap:
            name: {{ include "codelens.fullname" $ }}-config
---
apiVersion: v1
kind: Service
metadata:
  name: {{ $full }}
  labels:
    {{- include "codelens.labels" $ | nindent 4 }}
    app.kubernetes.io/component: {{ $name }}
    {{- if $spec.metrics }}
    codelens.io/scrape: "true"          {{/* selector для ServiceMonitor: только сервисы с /metrics */}}
    {{- end }}
spec:
  selector:
    {{- include "codelens.selectorLabels" $ | nindent 4 }}
    app.kubernetes.io/component: {{ $name }}
  ports:
    - name: http                        {{/* именованный порт - на него ссылается ServiceMonitor */}}
      port: {{ $spec.port }}
      targetPort: {{ $spec.port }}
{{- if and $spec.hpa $spec.hpa.enabled }}
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ $full }}
  labels:
    {{- include "codelens.labels" $ | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ $full }}
  minReplicas: {{ $spec.hpa.minReplicas }}
  maxReplicas: {{ $spec.hpa.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ $spec.hpa.cpu }}
{{- end }}
{{- end }}
{{- end -}}
