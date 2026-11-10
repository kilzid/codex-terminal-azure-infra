param location string
param workspaceName string
param auditDays int = 30
@secure()
param ownerObjectId string
resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  tags: { application: 'codex-terminal', owner: ownerObjectId }
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: auditDays
    workspaceCapping: { dailyQuotaGb: json('0.1') }
    features: { enableLogAccessUsingOnlyResourcePermissions: true }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}
