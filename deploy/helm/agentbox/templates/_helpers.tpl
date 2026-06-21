{{/*
Pod spec for the control plane, shared by the Deployment (stable) and the
Argo Rollouts Rollout (canary). Keeping it in one place means the two render
identical pods — the only thing that differs is the rollout strategy around them.
*/}}
{{- define "agentbox.podSpec" -}}
serviceAccountName: agentbox-controller
automountServiceAccountToken: true  # required: controller calls K8s API to manage Jobs
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  seccompProfile: {type: RuntimeDefault}
containers:
  - name: api
    image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
    imagePullPolicy: {{ .Values.image.pullPolicy }}
    ports: [{containerPort: 8080, name: http}]
    env:
      - name: AGENTBOX_DATABASE_URL
        valueFrom:
          secretKeyRef:
            name: {{ .Values.database.existingSecret }}
            key: {{ .Values.database.secretKey }}
      - name: AGENTBOX_EXEC_NAMESPACE
        value: {{ .Values.execNamespace }}
    readinessProbe:
      httpGet: {path: /readyz, port: http}
      periodSeconds: 5
    livenessProbe:
      httpGet: {path: /healthz, port: http}
      periodSeconds: 10
    resources:
{{ toYaml .Values.resources | indent 6 }}
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: {drop: [ALL]}
{{- end -}}
