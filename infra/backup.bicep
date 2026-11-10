param location string
param prefix string
param vmId string
param tags object = {}
param vmName string
param dailyRetentionDays int = 7
param instantRetentionDays int = 2

resource vault 'Microsoft.RecoveryServices/vaults@2024-04-01' = {
  name: '${prefix}-backup'
  location: location
  tags: tags
  sku: { name: 'RS0', tier: 'Standard' }
  properties: { publicNetworkAccess: 'Enabled' }
}
resource policy 'Microsoft.RecoveryServices/vaults/backupPolicies@2024-04-01' = {
  parent: vault
  name: 'vm-daily-seven-days'
  properties: {
    backupManagementType: 'AzureIaasVM'
    instantRpRetentionRangeInDays: instantRetentionDays
    timeZone: 'UTC'
    schedulePolicy: {
      schedulePolicyType: 'SimpleSchedulePolicy'
      scheduleRunFrequency: 'Daily'
      scheduleRunTimes: ['2020-01-01T03:00:00Z']
    }
    retentionPolicy: {
      retentionPolicyType: 'LongTermRetentionPolicy'
      dailySchedule: {
        retentionTimes: ['2020-01-01T03:00:00Z']
        retentionDuration: { count: dailyRetentionDays, durationType: 'Days' }
      }
    }
  }
}
resource protectedVM 'Microsoft.RecoveryServices/vaults/backupFabrics/protectionContainers/protectedItems@2024-04-01' = {
  name: '${vault.name}/Azure/iaasvmcontainer;iaasvmcontainerv2;${resourceGroup().name};${vmName}/vm;iaasvmcontainerv2;${resourceGroup().name};${vmName}'
  properties: { protectedItemType: 'Microsoft.Compute/virtualMachines', policyId: policy.id, sourceResourceId: vmId }
}
