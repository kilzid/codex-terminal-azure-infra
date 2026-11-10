param location string
param storageName string
param prefix string
param fileShareBackups bool = false
@secure()
param ownerObjectId string
param dailyRetentionDays int = 7
param fileBackupItemNames array = []
@secure()
param workspaceMetadata object = {}
@secure()
param configMetadata object = {}

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
// Preserve only Azure Backup's service-managed annotation namespace. The CLI
// reads and validates these operational artifacts; they are never stored in Git.
resource workspaceShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2024-01-01' = {
  parent: files
  name: 'workspace'
  properties: { shareQuota: 20, metadata: workspaceMetadata, enabledProtocols: 'SMB', accessTier: 'TransactionOptimized' }
}
resource configShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2024-01-01' = {
  parent: files
  name: 'codex-config'
  properties: { shareQuota: 20, metadata: configMetadata, enabledProtocols: 'SMB', accessTier: 'TransactionOptimized' }
}
resource vault 'Microsoft.RecoveryServices/vaults@2024-04-01' existing = { name: '${prefix}-backup' }
resource policy 'Microsoft.RecoveryServices/vaults/backupPolicies@2024-04-01' = if (fileShareBackups) {
  parent: vault
  name: 'daily-seven-days'
  properties: {
    backupManagementType: 'AzureStorage'
    workLoadType: 'AzureFileShare'
    timeZone: 'UTC'
    schedulePolicy: { schedulePolicyType: 'SimpleSchedulePolicy', scheduleRunFrequency: 'Daily', scheduleWeeklyFrequency: 0, scheduleRunTimes: ['2020-01-01T02:00:00Z'] }
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
resource protectedShares 'Microsoft.RecoveryServices/vaults/backupFabrics/protectionContainers/protectedItems@2024-04-01' = [for (itemName, i) in fileBackupItemNames: if (fileShareBackups) {
  parent: container
  name: itemName
  properties: any({ protectedItemType: 'AzureFileShareProtectedItem', fileShareProtocol: 'SMB', friendlyName: ['workspace', 'codex-config'][i], sourceResourceId: storage.id, policyId: policy.id, isInlineInquiry: true })
}]
