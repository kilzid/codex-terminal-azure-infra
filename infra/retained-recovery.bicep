param location string
param storageName string
param prefix string
param fileShareBackups bool = false
@secure()
param ownerObjectId string
param dailyRetentionDays int = 7

var tags = { application: 'codex-terminal', owner: ownerObjectId }
resource storage 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: storageName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true
    accessTier: 'Hot'
    networkAcls: { defaultAction: 'Deny', bypass: 'AzureServices', ipRules: [], virtualNetworkRules: [] }
  }
}
resource files 'Microsoft.Storage/storageAccounts/fileServices@2024-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    protocolSettings: { smb: { versions: 'SMB3.1.1', channelEncryption: 'AES-128-GCM;AES-256-GCM' } }
    shareDeleteRetentionPolicy: { enabled: true, days: 14 }
  }
}
resource shares 'Microsoft.Storage/storageAccounts/fileServices/shares@2024-01-01' = [for name in ['workspace', 'codex-config']: {
  parent: files
  name: name
  properties: { shareQuota: 20, enabledProtocols: 'SMB', accessTier: 'TransactionOptimized' }
}]
resource vault 'Microsoft.RecoveryServices/vaults@2024-04-01' existing = { name: '${prefix}-backup' }
resource policy 'Microsoft.RecoveryServices/vaults/backupPolicies@2024-04-01' = if (fileShareBackups) {
  parent: vault
  name: 'daily-seven-days'
  properties: {
    backupManagementType: 'AzureStorage'
    workLoadType: 'AzureFileShare'
    timeZone: 'UTC'
    schedulePolicy: { schedulePolicyType: 'SimpleSchedulePolicy', scheduleRunFrequency: 'Daily', scheduleRunTimes: ['2020-01-01T02:00:00Z'] }
    retentionPolicy: {
      retentionPolicyType: 'LongTermRetentionPolicy'
      dailySchedule: { retentionTimes: ['2020-01-01T02:00:00Z'], retentionDuration: { count: dailyRetentionDays, durationType: 'Days' } }
    }
  }
}
resource container 'Microsoft.RecoveryServices/vaults/backupFabrics/protectionContainers@2024-04-01' = if (fileShareBackups) {
  name: '${vault.name}/Azure/storagecontainer;Storage;${resourceGroup().name};${storageName}'
  properties: { backupManagementType: 'AzureStorage', containerType: 'StorageContainer', sourceResourceId: storage.id }
}
resource protectedShares 'Microsoft.RecoveryServices/vaults/backupFabrics/protectionContainers/protectedItems@2024-04-01' = [for shareName in ['workspace', 'codex-config']: if (fileShareBackups) {
  parent: container
  name: 'AzureFileShare;${shareName}'
  properties: any({ protectedItemType: 'AzureFileShareProtectedItem', sourceResourceId: storage.id, policyId: policy.id, isInlineInquiry: true })
}]
