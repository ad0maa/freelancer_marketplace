require 'rails_helper'

RSpec.describe 'Api::V1::Clients', type: :request do
  describe 'GET /api/v1/clients' do
    it 'returns a list of clients' do
      create_list(:client, 3)

      get '/api/v1/clients'

      expect(response).to have_http_status(:ok)
      json = JSON.parse(response.body)
      expect(json.length).to eq(3)
    end

    it 'returns an empty array when no clients exist' do
      get '/api/v1/clients'

      expect(response).to have_http_status(:ok)
      json = JSON.parse(response.body)
      expect(json).to eq([])
    end
  end

  describe 'GET /api/v1/clients/:id' do
    it 'returns a single client' do
      client = create(:client)

      get "/api/v1/clients/#{client.id}"

      expect(response).to have_http_status(:ok)
      json = JSON.parse(response.body)
      expect(json['name']).to eq(client.name)
      expect(json['email']).to eq(client.email)
    end

    it 'returns 404 when client not found' do
      get '/api/v1/clients/999'

      expect(response).to have_http_status(:not_found)
    end
  end

  describe 'POST /api/v1/clients' do
    it 'creates a new client with valid params' do
      params = { client: { name: 'John Doe', email: 'john@example.com' } }

      post '/api/v1/clients', params: params

      expect(response).to have_http_status(:created)
      json = JSON.parse(response.body)
      expect(json['name']).to eq('John Doe')
      expect(json['email']).to eq('john@example.com')
    end

    it 'returns errors with missing params' do
      params = { client: { name: '', email: '' } }

      post '/api/v1/clients', params: params

      expect(response).to have_http_status(:unprocessable_content)
      json = JSON.parse(response.body)
      expect(json['errors']).to be_present
    end

    it 'returns errors when email is malformed' do
      params = { client: { name: 'John', email: 'not-an-email' } }

      post '/api/v1/clients', params: params

      expect(response).to have_http_status(:unprocessable_content)
      json = JSON.parse(response.body)
      expect(json['errors']).to be_present
    end

    it 'returns errors when email is already taken' do
      create(:client, email: 'john@example.com')
      params = { client: { name: 'Other John', email: 'john@example.com' } }

      post '/api/v1/clients', params: params

      expect(response).to have_http_status(:unprocessable_content)
    end
  end

  describe 'PATCH /api/v1/clients/:id' do
    it 'updates an existing client' do
      client = create(:client)
      params = { client: { name: 'Updated Name' } }

      patch "/api/v1/clients/#{client.id}", params: params

      expect(response).to have_http_status(:ok)
      json = JSON.parse(response.body)
      expect(json['name']).to eq('Updated Name')
    end

    it 'returns errors with invalid params' do
      client = create(:client)
      params = { client: { name: '', email: '' } }

      patch "/api/v1/clients/#{client.id}", params: params

      expect(response).to have_http_status(:unprocessable_content)
    end

    it 'returns 404 when client not found' do
      patch '/api/v1/clients/999', params: { client: { name: 'Ghost' } }

      expect(response).to have_http_status(:not_found)
    end
  end

  describe 'DELETE /api/v1/clients/:id' do
    it 'deletes a client' do
      client = create(:client)

      delete "/api/v1/clients/#{client.id}"

      expect(response).to have_http_status(:no_content)
      expect(Client.find_by(id: client.id)).to be_nil
    end

    it 'returns 404 when client not found' do
      delete '/api/v1/clients/999'

      expect(response).to have_http_status(:not_found)
    end
  end
end
