{{ define "deployment-dagit" }}

{{- $userDeployments := index .Values "dagster-user-deployments" }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "dagster.dagit.fullname" . }}
  labels:
    {{- include "dagster.labels" . | nindent 4 }}
    component: dagit
  annotations:
    {{- range $key, $value := .Values.dagit.annotations }}
    {{ $key }}: {{ $value | squote }}
    {{- end }}
spec:
  replicas: {{ .Values.dagit.replicaCount }}
  selector:
    matchLabels:
      {{- include "dagster.selectorLabels" . | nindent 6 }}
      component: dagit
  template:
    metadata:
      labels:
        {{- include "dagster.selectorLabels" . | nindent 8 }}
        component: dagit
      annotations:
        checksum/dagster-workspace: {{ include (print $.Template.BasePath "/configmap-workspace.yaml") . | sha256sum }}
        checksum/dagster-instance: {{ include (print $.Template.BasePath "/configmap-instance.yaml") . | sha256sum }}
        {{- range $key, $value := .Values.dagit.annotations }}
        {{ $key }}: {{ $value | squote }}
        {{- end }}
    spec:
    {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
    {{- end }}
      serviceAccountName: {{ include "dagster.serviceAccountName" . }}
      securityContext:
        {{- toYaml .Values.dagit.podSecurityContext | nindent 8 }}
      initContainers:
        - name: check-db-ready
          image: {{ include "image.name" .Values.postgresql.image | quote }}
          imagePullPolicy: {{ .Values.postgresql.image.pullPolicy }}
          command: ['sh', '-c', {{ include "dagster.postgresql.pgisready" . | squote }}]
          securityContext:
            {{- toYaml .Values.dagit.securityContext | nindent 12 }}
        {{- if (and $userDeployments.enabled $userDeployments.enableSubchart) }}
        {{- range $deployment := $userDeployments.deployments }}
        - name: "init-user-deployment-{{- $deployment.name -}}"
          image: {{ include "image.name" $.Values.busybox.image | quote }}
          command: ['sh', '-c', "until nslookup {{ $deployment.name -}}; do echo waiting for user service; sleep 2; done"]
        {{- end }}
        {{- end }}
      containers:
        - name: {{ .Chart.Name }}
          securityContext:
            {{- toYaml .Values.dagit.securityContext | nindent 12 }}
          imagePullPolicy: {{ .Values.dagit.image.pullPolicy }}
          image: {{ include "image.name" .Values.dagit.image | quote }}
          command: [
            "/bin/bash",
            "-c",
            "{{ template "dagster.dagit.dagitCommand" $ }} {{ .dagitExtraCommandArgs -}}"
          ]
          env:
            - name: DAGSTER_PG_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ include "dagster.postgresql.secretName" . | quote }}
                  key: postgresql-password
          envFrom:
            - configMapRef:
                name: {{ template "dagster.fullname" . }}-dagit-env
            {{- range $envConfigMap := .Values.dagit.envConfigMaps }}
            - configMapRef: {{- $envConfigMap | toYaml | nindent 16 }}
            {{- end }}
            {{- range $envSecret := .Values.dagit.envSecrets }}
            - secretRef: {{- $envSecret | toYaml | nindent 16 }}
            {{- end }}
          volumeMounts:
            - name: dagster-instance
              mountPath: "{{ .Values.global.dagsterHome }}/dagster.yaml"
              subPath: dagster.yaml
            {{- if $userDeployments.enabled }}
            - name: dagster-workspace-yaml
              mountPath: "/dagster-workspace/workspace.yaml"
              subPath: workspace.yaml
            {{- end }}
          ports:
            - name: http
              containerPort: {{ .Values.dagit.service.port }}
              protocol: TCP
          resources:
            {{- toYaml .Values.dagit.resources | nindent 12 }}
        {{- if .Values.dagit.livenessProbe }}
          livenessProbe:
            {{- toYaml .Values.dagit.livenessProbe | nindent 12 }}
        {{- end }}
        {{- if .Values.dagit.startupProbe.enabled}}
          {{- $startupProbe := omit .Values.dagit.startupProbe "enabled" }}
          startupProbe:
            {{- toYaml $startupProbe | nindent 12 }}
        {{- end }}
      {{- with .Values.dagit.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}

      volumes:
        - name: dagster-instance
          configMap:
            name: {{ template "dagster.fullname" . }}-instance
        {{- if $userDeployments.enabled }}
        - name: dagster-workspace-yaml
          configMap:
            name: {{ template "dagster.fullname" . }}-workspace-yaml
        {{- end }}
    {{- with .Values.dagit.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
    {{- end }}
    {{- with .Values.dagit.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
    {{- end }}

{{ end }}
