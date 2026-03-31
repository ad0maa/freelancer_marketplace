require 'rails_helper'

RSpec.describe 'Api::V1::Freelancers', type: :request do
  describe 'GET /api/v1/freelancers' do
    it 'returns a list of freelancers' do
      create_list(:freelancer, 3)

      get '/api/v1/freelancers'

      expect(response).to have_http_status(:ok)
      json = JSON.parse(response.body)
      expect(json.length).to eq(3)
    end

    it 'returns an empty array when no freelancers exist' do
      get '/api/v1/freelancers'

      expect(response).to have_http_status(:ok)
      json = JSON.parse(response.body)
      expect(json.length).to eq(0)
    end
  end

  describe 'GET /api/v1/freelancers/:id' do
    it 'returns a single freelancer' do
      freelancer = create(:freelancer)

      get "/api/v1/freelancers/#{freelancer.id}"

      expect(response).to have_http_status(:ok)
      json = JSON.parse(response.body)
      expect(json['name']).to eq(freelancer.name)
      expect(json['email']).to eq(freelancer.email)
    end

    it 'returns 404 when freelancer not found' do
      get '/api/v1/freelancers/999'

      expect(response).to have_http_status(:not_found)
    end
  end

  describe 'POST /api/v1/freelancers' do
    it 'creates a new freelancer with valid params' do
      params = {
        freelancer: {
          name: 'Jane Smith',
          email: 'jane@example.com',
          hourly_rate: 75.00
        }
      }

      post '/api/v1/freelancers', params: params

      expect(response).to have_http_status(:created)
      json = JSON.parse(response.body)
      expect(json['name']).to eq('Jane Smith')
      expect(json['email']).to eq('jane@example.com')
    end

    it 'returns errors with invalid params' do
      params = { freelancer: { name: '', email: '' } }

      post '/api/v1/freelancers', params: params

      expect(response).to have_http_status(:unprocessable_entity)
      json = JSON.parse(response.body)
      expect(json['errors']).to be_present
    end
  end

  describe 'PATCH /api/v1/freelancers/:id' do
    it 'updates an existing freelancer' do
      freelancer = create(:freelancer)
      params = { freelancer: { name: 'Updated Name' } }

      patch "/api/v1/freelancers/#{freelancer.id}", params: params

      expect(response).to have_http_status(:ok)
      json = JSON.parse(response.body)
      expect(json['name']).to eq('Updated Name')
    end
  end

  describe 'DELETE /api/v1/freelancers/:id' do
    it 'deletes a freelancer' do
      freelancer = create(:freelancer)

      delete "/api/v1/freelancers/#{freelancer.id}"

      expect(response).to have_http_status(:no_content)
      expect(Freelancer.find_by(id: freelancer.id)).to be_nil
    end
  end
end
