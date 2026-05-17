$neo4jHome = 'C:\Users\KAVEN\.Neo4jDesktop\relate-data\dbmss\dbms-8a7ac7dd-19f7-47a2-969a-78ae8486d280'
$neo4jBat = Join-Path $neo4jHome 'bin\neo4j.bat'
Start-Process -FilePath $neo4jBat -ArgumentList 'console' -WorkingDirectory $neo4jHome -WindowStyle Hidden
Write-Host 'Neo4j start issued'
